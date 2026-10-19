"""Local HTTP server for the OSVerify GUI (standard library only)."""

from __future__ import annotations

import argparse
import importlib.util
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from osverifier.backend import DEFAULT_MODEL, MIN_CODEX_VERSION, available_models, stop_process
from osverifier.exporting import export_report
from osverifier.frozen import python_module_command
from osverifier.storage import atomic_text, read_json
from osverifier_reports.paper_prepare import MODES
from osverifier_reports.writer import source_snapshot

from . import runs as run_tools
from .jobs import JobManager
from .replay import ReplayError, rebuild_command
from .settings import (
    load_settings,
    paper_argv,
    prove_argv,
    save_settings,
    validate_settings,
    verify_argv,
    with_max_rounds,
)

STATIC = Path(__file__).parent / "static"
DEFAULT_HOME = Path("~/.osverifier").expanduser()
# Names a browser may use for this server. Anything else in the Host header means the
# request was routed here under a foreign name (DNS rebinding) and is refused.
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")


def is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class ApiError(Exception):
    def __init__(self, status: HTTPStatus, message: str):
        super().__init__(message)
        self.status = status


class CodexTools:
    """Version/login checks and a background ``codex login`` for the configured CLI."""

    def __init__(self, home: Path):
        self.home = home
        self._login: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def _environment(self, settings: dict) -> dict:
        environment = os.environ.copy()
        if settings["codex_home"]:
            codex_home = Path(settings["codex_home"]).expanduser().resolve()
            # Codex refuses a CODEX_HOME that does not exist; the app's own account
            # directory is created on first use so a fresh install can sign in.
            codex_home.mkdir(parents=True, exist_ok=True)
            environment["CODEX_HOME"] = str(codex_home)
        return environment

    def status(self, settings: dict) -> dict:
        binary = shutil.which(settings["codex_bin"])
        result = {
            "codex_bin": settings["codex_bin"],
            "path": binary,
            "version": None,
            "logged_in": None,
            "login_output": None,
            "error": None,
            "prove_available": importlib.util.find_spec("fastmcp") is not None,
            "models": available_models(settings.get("codex_home")),
            "default_model": DEFAULT_MODEL,
        }
        if binary is None:
            result["error"] = "Codex CLI not found; install it or set its path in Settings"
            return result
        environment = self._environment(settings)
        try:
            version = subprocess.run(
                [binary, "--version"], capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=15, env=environment
            )
            result["version"] = (version.stdout or version.stderr).strip()
            found = re.search(r"(\d+)\.(\d+)\.(\d+)", result["version"])
            if found and tuple(map(int, found.groups())) < MIN_CODEX_VERSION:
                minimum = ".".join(map(str, MIN_CODEX_VERSION))
                result["error"] = (
                    f"Codex CLI {found.group(0)} is too old; {minimum} or newer is required "
                    "(current models such as gpt-6-astra need it). Update it or choose a newer Codex in Settings."
                )
            login = subprocess.run(
                [binary, "login", "status"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                env=environment,
                stdin=subprocess.DEVNULL,
            )
            output = (login.stdout + login.stderr).strip()
            result["login_output"] = output
            result["logged_in"] = login.returncode == 0 and "not logged in" not in output.lower()
        except (OSError, subprocess.SubprocessError) as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    def start_login(self, settings: dict) -> dict:
        """Browser sign-in only: ``codex login`` opens the web page itself."""
        binary = shutil.which(settings["codex_bin"])
        if binary is None:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Codex CLI not found")
        with self._lock:
            if self._login is None or self._login.poll() is not None:
                log = self.home / "codex-login.log"
                log.write_text("", encoding="utf-8")
                handle = log.open("a", encoding="utf-8")
                try:
                    self._login = subprocess.Popen(
                        [binary, "login"],
                        stdin=subprocess.DEVNULL,
                        stdout=handle,
                        stderr=subprocess.STDOUT,
                        env=self._environment(settings),
                        start_new_session=True,
                    )
                finally:
                    handle.close()
        # Outside the lock: login_state() takes it again.
        return self.login_state()

    def stop_login(self) -> dict:
        with self._lock:
            process = self._login
        if process is not None and process.poll() is None:
            stop_process(process)
        return self.login_state()

    def login_state(self) -> dict:
        log = self.home / "codex-login.log"
        with self._lock:
            process = self._login
        output = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
        urls = re.findall(r"https?://\S+", output)
        return {
            "running": process is not None and process.poll() is None,
            "exit_code": None if process is None else process.poll(),
            "output": output,
            # The sign-in page Codex asks the user to open when no browser appeared.
            "url": next(
                (
                    u.rstrip(".,)")
                    for u in urls
                    if urlsplit(u).hostname not in ("localhost", "127.0.0.1")
                ),
                None,
            ),
        }


def _version() -> str:
    from rethlas_osv import __version__

    return __version__


class App:
    def __init__(self, home: Path, runs_root: Path, *, defaults: dict | None = None, desktop=False):
        self.home = home
        self.runs_root = runs_root
        self.defaults = defaults or {}
        self.desktop = desktop
        self.quit_requested = threading.Event()
        self.home.mkdir(parents=True, exist_ok=True)
        self.runs_root.mkdir(parents=True, exist_ok=True)
        self.settings_path = home / "settings.json"
        self.jobs = JobManager()
        self.codex = CodexTools(home)
        self._settings_lock = threading.Lock()
        self.settings_error: str | None = None

    # ---- settings -------------------------------------------------------------
    def settings(self) -> dict:
        with self._settings_lock:
            try:
                settings = load_settings(self.settings_path, self.defaults)
                self.settings_error = None
                return settings
            except ValueError as exc:
                # A corrupt file must not lock the user out; report it and use defaults.
                self.settings_error = str(exc)
                return validate_settings(self.defaults)

    def update_settings(self, raw: dict) -> dict:
        with self._settings_lock:
            try:
                settings = save_settings(self.settings_path, raw, self.defaults)
            except ValueError as exc:
                raise ApiError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
            self.settings_error = None
            return settings

    def state(self) -> dict:
        return {
            "home": str(self.home),
            "runs_root": str(self.runs_root),
            "python": sys.executable,
            "settings": self.settings(),
            "settings_error": self.settings_error,
            "desktop": self.desktop,
            "version": _version(),
            "active_runs": self.jobs.shutdown_note(),
        }

    # ---- runs -----------------------------------------------------------------
    def run_dir(self, name: str) -> Path:
        try:
            directory = run_tools.safe_run_dir(self.runs_root, name)
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
        if not directory.is_dir():
            raise ApiError(HTTPStatus.NOT_FOUND, "run not found")
        return directory

    def create_run(self, body: dict) -> dict:
        kind = body.get("kind")
        if kind not in ("verify", "prove"):
            raise ApiError(HTTPStatus.BAD_REQUEST, "kind must be verify or prove")
        name = str(body.get("name") or "").strip()
        if not name:
            raise ApiError(HTTPStatus.BAD_REQUEST, "give the run a name")
        base = self.settings()
        overrides = body.get("settings") or {}
        if not isinstance(overrides, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "settings must be an object")
        merged = {
            **base,
            **{k: overrides[k] for k in ("codex_bin", "codex_home") if k in overrides},
            kind: {**base[kind], **(overrides.get(kind) or {})},
        }
        try:
            settings = validate_settings(merged)
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
        if kind == "prove" and importlib.util.find_spec("fastmcp") is None:
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "proving needs the rethlas extra: run `uv sync --extra rethlas`",
            )
        directory = run_tools.unique_run_dir(self.runs_root, name)
        inputs = directory / run_tools.INPUTS_DIR
        if kind == "verify" and body.get("paper") is not None:
            paper = str(body.get("paper") or "")
            mode = body.get("mode")
            if not paper.strip():
                raise ApiError(HTTPStatus.BAD_REQUEST, "the paper text is required")
            if mode not in MODES:
                raise ApiError(HTTPStatus.BAD_REQUEST, "mode must be smart, lazy, or manual")
            tags = [
                t.strip() for t in re.split(r"[,\s]+", str(body.get("tags") or "")) if t.strip()
            ]
            if mode == "manual" and not tags:
                raise ApiError(HTTPStatus.BAD_REQUEST, "manual mode needs at least one LaTeX label")
            if mode != "manual":
                tags = []
            filename = str(body.get("paper_name") or "")
            suffix = (
                ".tex"
                if filename.lower().endswith(".tex") or "\\begin{document}" in paper
                else ".md"
            )
            paper_path = inputs / f"paper{suffix}"
            atomic_text(paper_path, paper)
            argv = paper_argv(directory, settings, paper_path, mode, tags, directory.name)
        elif kind == "verify":
            question = str(body.get("question") or "")
            solution = str(body.get("solution") or "")
            if not question.strip() or not solution.strip():
                raise ApiError(
                    HTTPStatus.BAD_REQUEST, "both the question and the solution are required"
                )
            atomic_text(inputs / "question.md", question)
            atomic_text(inputs / "solution.md", solution)
            argv = verify_argv(
                directory, settings, inputs / "question.md", inputs / "solution.md", directory.name
            )
        else:
            problem = str(body.get("problem") or "")
            if not problem.strip():
                raise ApiError(HTTPStatus.BAD_REQUEST, "the problem statement is required")
            atomic_text(inputs / "problem.md", problem)
            argv = prove_argv(directory, settings, inputs / "problem.md")
        try:
            self.jobs.start(directory, kind, argv, settings, resume=False)
        except (OSError, RuntimeError) as exc:
            raise ApiError(HTTPStatus.CONFLICT, str(exc)) from exc
        return run_tools.run_summary(directory, self.jobs)

    def resume_run(self, name: str, body: dict) -> dict:
        directory = self.run_dir(name)
        job = run_tools.read_job(directory)
        if job is None:
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "this run was not started from the GUI; resume it with its original command",
            )
        # Never run the stored argv itself: the run folder may come from someone else.
        settings = self.settings()
        try:
            argv = rebuild_command(
                job,
                directory,
                codex_bins=(settings["codex_bin"], self.defaults.get("codex_bin")),
                codex_homes=(settings["codex_home"], self.defaults.get("codex_home")),
            )
        except ReplayError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"cannot resume this run: {exc}") from exc
        if job["kind"] == "prove" and body.get("max_rounds") not in (None, ""):
            try:
                argv = with_max_rounds(argv, int(body["max_rounds"]))
            except (TypeError, ValueError) as exc:
                raise ApiError(
                    HTTPStatus.BAD_REQUEST, "max_rounds must be a positive integer"
                ) from exc
        try:
            self.jobs.start(directory, job["kind"], argv, job.get("settings", {}), resume=True)
        except (OSError, RuntimeError) as exc:
            raise ApiError(HTTPStatus.CONFLICT, str(exc)) from exc
        return run_tools.run_summary(directory, self.jobs)

    def cancel_run(self, name: str) -> dict:
        directory = self.run_dir(name)
        try:
            self.jobs.cancel(directory)
        except RuntimeError as exc:
            raise ApiError(HTTPStatus.CONFLICT, str(exc)) from exc
        return run_tools.run_summary(directory, self.jobs)


ROUTES = [
    ("GET", re.compile(r"^/api/runs/(?P<name>[^/]+)/template-pdf$"), "get_template_pdf"),
    ("POST", re.compile(r"^/api/runs/(?P<name>[^/]+)/report-draft$"), "post_report_draft"),
    ("GET", re.compile(r"^/api/report-drafts/(?P<draft_id>[a-f0-9]{32})$"), "get_report_draft"),
    ("GET", re.compile(r"^/api/report-drafts/(?P<draft_id>[a-f0-9]{32})/download$"), "get_report_draft_download"),
    ("GET", re.compile(r"^/api/runs/(?P<name>[^/]+)/export$"), "get_export"),
    ("GET", re.compile(r"^/api/runs/(?P<name>[^/]+)/reviews/(?P<key>[^/]+)/export$"), "get_export"),
    ("GET", re.compile(r"^/api/state$"), "get_state"),
    ("POST", re.compile(r"^/api/quit$"), "post_quit"),
    ("GET", re.compile(r"^/api/settings$"), "get_settings"),
    ("PUT", re.compile(r"^/api/settings$"), "put_settings"),
    ("GET", re.compile(r"^/api/codex/status$"), "get_codex_status"),
    ("GET", re.compile(r"^/api/codex/login$"), "get_codex_login"),
    ("POST", re.compile(r"^/api/codex/login$"), "post_codex_login"),
    ("POST", re.compile(r"^/api/codex/login/cancel$"), "post_codex_login_cancel"),
    ("GET", re.compile(r"^/api/runs$"), "get_runs"),
    ("POST", re.compile(r"^/api/runs$"), "post_runs"),
    ("GET", re.compile(r"^/api/runs/(?P<name>[^/]+)$"), "get_run"),
    ("POST", re.compile(r"^/api/runs/(?P<name>[^/]+)/cancel$"), "post_cancel"),
    ("POST", re.compile(r"^/api/runs/(?P<name>[^/]+)/resume$"), "post_resume"),
    ("GET", re.compile(r"^/api/runs/(?P<name>[^/]+)/events$"), "get_events"),
    ("GET", re.compile(r"^/api/runs/(?P<name>[^/]+)/reviews/(?P<key>[^/]+)$"), "get_review"),
]


class Handler(BaseHTTPRequestHandler):
    app: App  # set on the server class
    server_version = "RethlasOSV/0.1"

    def log_message(self, format, *args):  # noqa: A002 - BaseHTTPRequestHandler signature
        if os.environ.get("OSVERIFIER_GUI_LOG"):
            super().log_message(format, *args)

    # ---- plumbing --------------------------------------------------------------
    def _send_json(self, payload, status=HTTPStatus.OK):
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            raise ApiError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "send application/json")
        try:
            body = json.loads(self.rfile.read(length))
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid JSON body") from exc
        if not isinstance(body, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "JSON body must be an object")
        return body

    def _check_host(self):
        address, port = self.server.server_address[:2]
        allowed = {f"{name}:{port}" for name in (*LOOPBACK_HOSTS, address)}
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in allowed:
            raise ApiError(
                HTTPStatus.FORBIDDEN,
                f"unexpected Host header; open the app at http://127.0.0.1:{port}/",
            )

    def _check_origin(self):
        # The server only binds to localhost; still refuse cross-site writes.
        origin = self.headers.get("Origin")
        if origin is None:
            return
        host = urlsplit(origin).netloc
        if host != self.headers.get("Host"):
            raise ApiError(HTTPStatus.FORBIDDEN, "cross-origin request refused")

    def _dispatch(self, method: str):
        parts = urlsplit(self.path)
        try:
            self._check_host()
            if method == "GET" and parts.path in ("/", "/index.html"):
                return self._send_static("index.html", "text/html; charset=utf-8")
            for route_method, pattern, name in ROUTES:
                match = pattern.match(parts.path)
                if match and route_method == method:
                    if method != "GET":
                        self._check_origin()
                    query = {k: v[-1] for k, v in parse_qs(parts.query).items()}
                    return getattr(self, name)(query=query, **match.groupdict())
            raise ApiError(HTTPStatus.NOT_FOUND, "no such endpoint")
        except ApiError as exc:
            self._send_json({"error": str(exc)}, exc.status)
        except Exception as exc:  # keep the server alive; report the failure
            self._send_json(
                {"error": f"{type(exc).__name__}: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR
            )

    def _send_static(self, name: str, content_type: str):
        data = (STATIC / name).read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        self._dispatch("GET")

    def do_POST(self):  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self):  # noqa: N802
        self._dispatch("PUT")

    # ---- endpoints -------------------------------------------------------------
    def get_state(self, query):
        self._send_json(self.app.state())

    def post_quit(self, query):
        # Runs are separate processes and keep going; only this server stops.
        # Answer first: once the event is set the process may exit at any moment.
        self._send_json({"quitting": True, "active_runs": self.app.jobs.shutdown_note()})
        self.wfile.flush()
        self.app.quit_requested.set()

    def get_settings(self, query):
        self._send_json(self.app.settings())

    def put_settings(self, query):
        self._send_json(self.app.update_settings(self._read_body()))

    def get_codex_status(self, query):
        self._send_json(self.app.codex.status(self.app.settings()))

    def get_codex_login(self, query):
        self._send_json(self.app.codex.login_state())

    def post_codex_login(self, query):
        self._send_json(self.app.codex.start_login(self.app.settings()))

    def post_codex_login_cancel(self, query):
        self._send_json(self.app.codex.stop_login())

    def get_runs(self, query):
        self._send_json({"runs": run_tools.list_runs(self.app.runs_root, self.app.jobs)})

    def post_runs(self, query):
        self._send_json(self.app.create_run(self._read_body()), HTTPStatus.CREATED)

    def get_template_pdf(self, query, name):
        from osverifier_reports.automatic import report_directory

        directory = report_directory(self.app.run_dir(name))
        if not (directory / "status.json").exists() or read_json(directory / "status.json").get("status") != "completed":
            raise ApiError(HTTPStatus.NOT_FOUND, "Template PDF is not ready")
        partial = read_json(directory / "status.json").get("partial", False)
        data = (directory / "partial" / "report.pdf" if partial else directory / "report.pdf").read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Disposition", 'attachment; filename="referee-report.pdf"')
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def get_run(self, query, name):
        detail = run_tools.run_detail(self.app.run_dir(name), self.app.jobs)
        if detail is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "not a verifier run directory")
        from osverifier_reports.automatic import report_directory

        source = self.app.run_dir(name)
        if (source / "report.json").exists():
            status_path = report_directory(source) / "status.json"
            if status_path.exists():
                detail["template_report"] = read_json(status_path)
                if detail["template_report"]["status"] == "running" and not self.app.jobs.is_active(source):
                    detail["template_report"] = {"status": "failed", "error": "Report process stopped; resume to retry."}
        self._send_json(detail)

    def post_cancel(self, query, name):
        self._send_json(self.app.cancel_run(name))

    def post_resume(self, query, name):
        self._send_json(self.app.resume_run(name, self._read_body()))

    def get_events(self, query, name):
        try:
            offset = max(0, int(query.get("offset", 0)))
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "offset must be an integer") from exc
        self._send_json(run_tools.trajectory_events(self.app.run_dir(name), offset))

    def post_report_draft(self, query, name):
        body = self._read_body()
        directory = self.app.run_dir(name)
        key = body.get("review")
        if key:
            if not isinstance(key, str) or "/" in key or "\\" in key or key.startswith("."):
                raise ApiError(HTTPStatus.BAD_REQUEST, "invalid review key")
            directory = directory / "reviews" / key
        if not directory.resolve().is_relative_to(self.app.runs_root.resolve()):
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid source path")
        try:
            source_snapshot(directory / "report.json")
        except (OSError, ValueError, KeyError) as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "A stopped verification with a released report is required") from exc
        settings = self.app.settings()
        draft_id = uuid.uuid4().hex
        output = self.app.home / "report-drafts" / draft_id
        argv = python_module_command("osverifier_reports.writer") + [
            "--run-dir", str(directory.resolve()), "--output-dir", str(output.resolve()),
            "--model", settings["verify"]["model"], "--codex-bin", settings["codex_bin"],
            # The GUI's chosen limits apply to editorial calls, as they do to verify runs.
            "--timeout-seconds", str(settings["verify"]["timeout_seconds"]),
            "--reasoning-effort", settings["verify"]["reasoning_effort"],
        ]
        if settings["codex_home"]:
            argv += ["--codex-home", settings["codex_home"]]
        self.app.jobs.start(output, "report", argv, settings, resume=False)
        self._send_json({"draft_id": draft_id}, HTTPStatus.ACCEPTED)

    def get_report_draft(self, query, draft_id):
        directory = self.app.home / "report-drafts" / draft_id
        if not directory.is_dir():
            raise ApiError(HTTPStatus.NOT_FOUND, "report draft not found")
        active = self.app.jobs.is_active(directory)
        status = read_json(directory / "status.json") if (directory / "status.json").exists() else {"status": "running" if active else "failed"}
        if status["status"] == "running" and not active:
            status = {"status": "failed", "error": "Report process stopped before completion; inspect the report output directory"}
        self._send_json({**status, "output_directory": str(directory)})

    def get_report_draft_download(self, query, draft_id):
        directory = self.app.home / "report-drafts" / draft_id
        if not (directory / "status.json").is_file() or read_json(directory / "status.json").get("status") != "completed":
            raise ApiError(HTTPStatus.NOT_FOUND, "No editorially approved report is available")
        data = (directory / "report.tex").read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/x-tex; charset=utf-8")
        self.send_header("Content-Disposition", 'attachment; filename="referee-report.tex"')
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def get_export(self, query, name, key=None):
        directory = self.app.run_dir(name)
        if key is not None:
            if "/" in key or "\\" in key or key.startswith("."):
                raise ApiError(HTTPStatus.BAD_REQUEST, "invalid review key")
            directory = directory / "reviews" / key
        if not directory.resolve().is_relative_to(self.app.runs_root.resolve()):
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid export path")
        try:
            text, filename, content_type = export_report(directory, query.get("format", "latex"))
        except FileNotFoundError:
            raise ApiError(HTTPStatus.NOT_FOUND, "No released report is available yet") from None
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
        data = text.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def get_review(self, query, name, key):
        try:
            detail = run_tools.review_detail(self.app.run_dir(name), key)
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
        if detail is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "review not found")
        from osverifier_reports.automatic import report_directory

        source = self.app.run_dir(name)
        if (source / "report.json").exists():
            status_path = report_directory(source) / "status.json"
            if status_path.exists():
                detail["template_report"] = read_json(status_path)
                if detail["template_report"]["status"] == "running" and not self.app.jobs.is_active(source):
                    detail["template_report"] = {"status": "failed", "error": "Report process stopped; resume to retry."}
        self._send_json(detail)


def make_server(app: App, host: str, port: int) -> ThreadingHTTPServer:
    if not is_loopback_host(host):
        # The API has no authentication: anyone who can reach it controls the user's runs,
        # settings and Codex subscription. Never listen beyond this machine.
        raise ValueError(f"refusing to listen on {host!r}: the GUI serves loopback only")
    handler = type("BoundHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Local web GUI for Rethlas-OSV")
    p.add_argument("--home", type=Path, default=DEFAULT_HOME, help="Settings and default runs")
    p.add_argument("--runs-dir", type=Path, help="Where run directories live (default: HOME/runs)")
    p.add_argument("--host", default="127.0.0.1", help="Loopback address to listen on")
    p.add_argument("--port", type=int, default=8765, help="0 picks a free port")
    p.add_argument("--no-browser", action="store_true")
    return p


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    if not is_loopback_host(args.host):
        parser().error("--host must be a loopback address such as 127.0.0.1 (the GUI has no login)")
    home = args.home.expanduser().resolve()
    runs_root = (args.runs_dir or home / "runs").expanduser().resolve()
    app = App(home, runs_root)
    try:
        server = make_server(app, args.host, args.port)
    except OSError:
        server = make_server(app, args.host, 0)
    url = f"http://{args.host}:{server.server_address[1]}/"
    print(f"Rethlas-OSV GUI: {url}  (settings: {home}, runs: {runs_root})", flush=True)
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        active = app.jobs.shutdown_note()
        if active:
            print("Runs still in progress keep running; reopen the GUI to watch them:", flush=True)
            for path in active:
                print(f"  {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
