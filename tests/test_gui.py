"""GUI plumbing: settings, exact CLI argv, run inspection, and process control.

A scripted fake ``codex`` binary stands in for the real CLI, so these tests establish
that jobs start, stop, and resume through the GUI. They say nothing about review quality.
"""

from __future__ import annotations

import json
import sys
import urllib.request

import pytest
from conftest import fake_codex, wait_for
from osverifier import cli as verifier_cli
from osverifier.storage import read_json

from rethlas_osv import cli as rethlas_cli
from rethlas_osv_gui import runs as run_tools
from rethlas_osv_gui.jobs import JobManager, read_job
from rethlas_osv_gui.settings import (
    DEFAULTS,
    load_settings,
    prove_argv,
    save_settings,
    validate_settings,
    verify_argv,
    with_max_rounds,
)

# ---- settings -------------------------------------------------------------------


def test_settings_roundtrip_and_validation(tmp_path):
    path = tmp_path / "settings.json"
    assert load_settings(path) == DEFAULTS
    saved = save_settings(
        path,
        {
            "codex_bin": " /opt/codex ",
            "verify": {"max_issues": "0", "workers": "3"},
            "prove": {"max_rounds": 4},
        },
    )
    assert saved["codex_bin"] == "/opt/codex"
    assert saved["verify"]["max_issues"] is None and saved["verify"]["workers"] == 3
    assert saved["prove"]["max_rounds"] == 4
    assert load_settings(path) == saved
    for bad in (
        {"verify": {"reasoning_effort": "max"}},
        {"verify": {"workers": 0}},
        {"verify": {"model": " "}},
        {"verify": {"decompose": False, "early_stop": False}},
        {"prove": {"search_policy": "sometimes"}},
        {"prove": {"unknown": 1}},
    ):
        with pytest.raises(ValueError):
            validate_settings(bad)
    path.write_text("{not json")
    with pytest.raises(ValueError):
        load_settings(path)


def test_argv_uses_exact_cli_flags(tmp_path):
    settings = validate_settings(
        {
            "codex_home": "/tmp/acct",
            "verify": {"max_issues": 5, "early_stop": False},
            "prove": {"decompose": False, "max_issues": 2},
        }
    )
    run = tmp_path / "run"
    argv = verify_argv(run, settings, tmp_path / "q.md", tmp_path / "s.md", "run")
    assert argv[:4] == [sys.executable, "-m", "osverifier.cli", "verify"]
    parsed = verifier_cli.parser().parse_args(argv[3:])
    assert parsed.decompose and parsed.no_early_stop and parsed.max_issues == 5
    assert parsed.reasoning_effort == "xhigh" and parsed.web_search == "live"
    assert str(parsed.codex_home) == "/tmp/acct" and parsed.workers == 16

    argv = prove_argv(run, settings, tmp_path / "p.md")
    assert argv[:3] == [sys.executable, "-m", "rethlas_osv.cli"]
    parsed = rethlas_cli.parser().parse_args(argv[3:])
    assert parsed.root_only and not parsed.no_early_stop and parsed.max_issues == 2
    assert parsed.generator_timeout == 0 and parsed.max_rounds == 10
    deeper = with_max_rounds(argv, 12)
    assert rethlas_cli.parser().parse_args(deeper[3:]).max_rounds == 12
    assert [a for a in deeper if a != "12"] == [a for a in argv if a != "10"]
    with pytest.raises(ValueError):
        with_max_rounds(argv, 0)


# ---- run inspection ---------------------------------------------------------------


def test_demo_run_is_listed_as_completed_verify(tmp_path):
    runs_root = tmp_path / "runs"
    assert verifier_cli.main(["demo", "--run-dir", str(runs_root / "demo")]) == 0
    (runs_root / "not-a-run").mkdir()
    manager = JobManager()
    rows = run_tools.list_runs(runs_root, manager)
    assert [r["name"] for r in rows] == ["demo"]
    row = rows[0]
    assert row["kind"] == "verify" and row["status"] == "completed" and not row["active"]
    assert row["decision"] == read_json(runs_root / "demo/report.json")["decision"]
    detail = run_tools.run_detail(runs_root / "demo", manager)
    assert detail["report_markdown"].startswith("# Verifier report")
    assert detail["inputs"]["question"] and detail["inputs"]["solution"]
    assert detail["job"] is None and detail["resumable"] is False


def test_trajectory_events_are_offset_paged(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    path = run / "trajectory.jsonl"
    path.write_text('{"sequence": 1, "event": "a"}\n{"sequence": 2, "event": "b"}\n')
    first = run_tools.trajectory_events(run, 0)
    assert [e["event"] for e in first["events"]] == ["a", "b"]
    with path.open("a") as out:
        out.write('{"sequence": 3, "event": "c"}\n{"sequence": 4, "ev')  # partial line
    second = run_tools.trajectory_events(run, first["offset"])
    assert [e["event"] for e in second["events"]] == ["c"]
    assert run_tools.trajectory_events(run, second["offset"])["events"] == []
    assert run_tools.trajectory_events(run, 10**9)["events"][0]["event"] == "a"


def test_run_names_are_confined_to_the_runs_root(tmp_path):
    for name in ("", "..", "a/b", ".hidden"):
        with pytest.raises(ValueError):
            run_tools.safe_run_dir(tmp_path, name)
    assert run_tools.unique_run_dir(tmp_path, "My Paper (v2)!").name == "my-paper-v2"
    (tmp_path / "my-paper-v2").mkdir()
    assert run_tools.unique_run_dir(tmp_path, "My Paper (v2)!").name == "my-paper-v2-2"


# ---- HTTP server with a fake Codex ------------------------------------------------


def test_server_state_settings_and_origin_check(served):
    app, client = served
    status, state = client.call("GET", "/api/state")
    assert (
        status == 200 and state["settings"] == DEFAULTS and state["runs_root"] == str(app.runs_root)
    )
    status, body = client.call("PUT", "/api/settings", {"verify": {"workers": 0}})
    assert status == 400 and "workers" in body["error"]
    status, body = client.call("PUT", "/api/settings", {"verify": {"workers": 2}})
    assert status == 200 and body["verify"]["workers"] == 2
    assert read_json(app.settings_path)["verify"]["workers"] == 2
    status, body = client.call(
        "PUT",
        "/api/settings",
        {"verify": {"workers": 3}},
        headers={"Origin": "http://evil.example"},
    )
    assert status == 403 and read_json(app.settings_path)["verify"]["workers"] == 2
    status, body = client.call(
        "POST", "/api/runs", {"kind": "verify", "name": "x", "question": "q"}
    )
    assert status == 400 and "solution" in body["error"]
    assert client.call("GET", "/api/runs/missing")[0] == 404
    with urllib.request.urlopen(client.base + "/", timeout=10) as response:
        assert response.status == 200 and b'id="tab-about"' in response.read()
    assert client.call("GET", "/api/runs/../etc")[0] in (400, 404)


def test_codex_status_uses_configured_binary(served, tmp_path):
    app, client = served
    client.call("PUT", "/api/settings", {"codex_bin": str(fake_codex(tmp_path))})
    status, body = client.call("GET", "/api/codex/status")
    assert status == 200 and body["version"] == "codex-cli 0.162.0-fake" and body["logged_in"] is True
    client.call("PUT", "/api/settings", {"codex_bin": str(tmp_path / "absent")})
    status, body = client.call("GET", "/api/codex/status")
    assert body["path"] is None and "not found" in body["error"]


def test_sign_in_reports_url_and_survives_repeated_clicks(served, tmp_path):
    app, client = served
    client.call("PUT", "/api/settings", {"codex_bin": str(fake_codex(tmp_path))})
    status, first = client.call("POST", "/api/codex/login", {})
    assert status == 200 and first["running"]
    pid = app.codex._login.pid
    state = wait_for(lambda: (s := client.call("GET", "/api/codex/login")[1]) and s["url"] and s)
    assert state["url"] == "https://auth.example/oauth?state=abc" and state["running"]
    # A second click must neither start a second process nor hang the server.
    status, again = client.call("POST", "/api/codex/login", {})
    assert status == 200 and again["running"] and again["url"] == state["url"]
    assert app.codex._login.pid == pid
    status, stopped = client.call("POST", "/api/codex/login/cancel")
    assert status == 200 and not stopped["running"]
    assert client.call("GET", "/api/codex/login")[1]["running"] is False


def test_verify_job_starts_cancels_and_resumes(served, tmp_path):
    app, client = served
    client.call("PUT", "/api/settings", {"codex_bin": str(fake_codex(tmp_path))})
    status, run = client.call(
        "POST",
        "/api/runs",
        {
            "kind": "verify",
            "name": "Paper One",
            "question": "Is 2 prime?",
            "solution": "Yes: its only divisors are 1 and 2.",
            "settings": {"verify": {"workers": 2, "reasoning_effort": "high"}},
        },
    )
    assert status == 201, run
    assert run["name"] == "paper-one" and run["status"] == "running" and run["kind"] == "verify"
    run_dir = app.runs_root / "paper-one"
    job = read_job(run_dir)
    assert job["settings"]["verify"]["workers"] == 2 and "--decompose" in job["argv"]
    assert (run_dir / "gui-inputs/question.md").read_text() == "Is 2 prime?"
    parsed = verifier_cli.parser().parse_args(job["argv"][3:])
    assert parsed.reasoning_effort == "high" and parsed.codex_bin == str(tmp_path / "bin" / "codex")

    # The real CLI freezes the manifest, then the fake Codex blocks inside the first job.
    wait_for(
        lambda: (run_dir / "manifest.json").exists() and (run_dir / "trajectory.jsonl").exists()
    )
    manifest = read_json(run_dir / "manifest.json")["provenance"]
    assert manifest["config"]["workers"] == 2 and manifest["backend"]["reasoning_effort"] == "high"
    assert manifest["request"]["candidate_solution"] == "Yes: its only divisors are 1 and 2."
    status, detail = client.call("GET", "/api/runs/paper-one")
    assert (
        detail["status"] == "running" and detail["active"] and detail["job"]["status"] == "running"
    )
    assert (
        client.call("GET", "/api/runs/paper-one/events?offset=0")[1]["events"][0]["event"]
        == "run_started"
    )
    assert client.call("POST", "/api/runs/paper-one/resume", {})[0] == 409

    # Cancel sends SIGINT like Ctrl+C: the CLI records the interruption and exits 130.
    status, body = client.call("POST", "/api/runs/paper-one/cancel")
    assert status == 200
    job = wait_for(lambda: (j := read_job(run_dir)) and j["status"] == "exited" and j)
    assert job["exit_code"] == 130 and job["cancel_requested_at"]
    status, detail = client.call("GET", "/api/runs/paper-one")
    assert (
        detail["status"] in ("incomplete", "interrupted")
        and detail["resumable"]
        and not detail["active"]
    )
    events = [
        e["event"] for e in client.call("GET", "/api/runs/paper-one/events?offset=0")[1]["events"]
    ]
    assert "run_interrupted" in events
    assert client.call("POST", "/api/runs/paper-one/cancel")[0] == 409

    # Resume replays the exact stored command against the frozen run directory.
    status, body = client.call("POST", "/api/runs/paper-one/resume", {})
    assert status == 200 and body["status"] == "running"
    resumed = read_job(run_dir)
    assert resumed["argv"] == job["argv"] and resumed["invocations"] == 2 and resumed["resume"]
    wait_for(lambda: len((run_dir / "trajectory.jsonl").read_text().splitlines()) > len(events))
    assert client.call("GET", "/api/runs")[1]["runs"][0]["status"] == "running"
    client.call("POST", "/api/runs/paper-one/cancel")
    wait_for(lambda: read_job(run_dir)["status"] == "exited")
    assert not app.jobs.is_active(run_dir)


def test_prove_job_records_problem_and_rounds_override(served, tmp_path):
    app, client = served
    client.call("PUT", "/api/settings", {"codex_bin": str(fake_codex(tmp_path))})
    status, run = client.call(
        "POST",
        "/api/runs",
        {
            "kind": "prove",
            "name": "conj",
            "problem": "Show every even n > 2 is a sum of two primes.",
        },
    )
    assert status == 201, run
    run_dir = app.runs_root / "conj"
    assert run["kind"] == "prove" and (run_dir / "gui-inputs/problem.md").exists()
    wait_for(lambda: (run_dir / "manifest.json").exists() and (run_dir / "summary.json").exists())
    assert read_json(run_dir / "manifest.json")["provenance"]["question"].startswith("Show every")
    assert (run_dir / "inputs/problem.md").read_text().startswith("Show every")
    client.call("POST", "/api/runs/conj/cancel")
    wait_for(lambda: read_job(run_dir)["status"] == "exited")
    status, detail = client.call("GET", "/api/runs/conj")
    assert detail["kind"] == "prove" and detail["resumable"] and detail["status"] != "running"
    status, body = client.call("POST", "/api/runs/conj/resume", {"max_rounds": 3})
    assert status == 200, body
    assert rethlas_cli.parser().parse_args(read_job(run_dir)["argv"][3:]).max_rounds == 3
    client.call("POST", "/api/runs/conj/cancel")
    wait_for(lambda: read_job(run_dir)["status"] == "exited")


def test_reattach_marks_lost_process_as_exited(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "gui-job.json").write_text(
        json.dumps(
            {"kind": "verify", "argv": [], "settings": {}, "pid": 2**22 - 1, "status": "running"}
        )
    )
    manager = JobManager()
    assert manager.is_active(run_dir) is False
    assert read_job(run_dir)["status"] == "exited" and read_job(run_dir)["exit_code"] is None
    with pytest.raises(RuntimeError):
        manager.cancel(run_dir)


def _raw(client, method, path, host, body=None, origin=None):
    import http.client

    port = int(client.base.rsplit(":", 1)[1])
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Host": host, "Content-Type": "application/json"}
    if origin:
        headers["Origin"] = origin
    data = None if body is None else json.dumps(body)
    connection.request(method, path, body=data, headers=headers)
    response = connection.getresponse()
    payload = response.read()
    connection.close()
    return response.status, payload


def test_dns_rebinding_host_names_are_refused(served):
    # A page on attacker.example that rebinds its name to 127.0.0.1 sends matching
    # Host and Origin headers; only the loopback names this server listens on pass.
    app, client = served
    port = client.base.rsplit(":", 1)[1]
    rebound = f"attacker.example:{port}"
    for path in ("/", "/api/state", "/api/runs"):
        status, _ = _raw(client, "GET", path, rebound)
        assert status == 403, path
    status, _ = _raw(
        client, "PUT", "/api/settings", rebound, {"codex_bin": "/bin/sh"}, f"http://{rebound}"
    )
    assert status == 403 and not app.settings_path.exists()
    assert _raw(client, "GET", "/api/state", f"127.0.0.1:{int(port) + 1}")[0] == 403
    for name in ("127.0.0.1", "localhost", "LOCALHOST"):
        assert _raw(client, "GET", "/api/state", f"{name}:{port}")[0] == 200, name


def test_server_refuses_non_loopback_addresses(tmp_path):
    from rethlas_osv_gui import server

    app = server.App(tmp_path / "home", tmp_path / "home" / "runs")
    for host in ("0.0.0.0", "192.168.1.5", "example.com"):
        with pytest.raises(ValueError):
            server.make_server(app, host, 0)
    with pytest.raises(SystemExit):
        server.main(["--home", str(tmp_path / "home"), "--host", "0.0.0.0", "--no-browser"])


def _plant_job(run_dir, argv, kind="verify"):
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "gui-job.json").write_text(
        json.dumps(
            {"kind": kind, "argv": argv, "settings": {}, "pid": None,
             "status": "exited", "exit_code": 130}
        )
    )


def test_resume_never_executes_a_tampered_stored_command(served, tmp_path):
    # A run folder copied from someone else carries its own gui-job.json.
    app, client = served
    marker = tmp_path / "executed"
    evil = app.runs_root / "shared" / "tool"
    _plant_job(app.runs_root / "shared", ["/usr/bin/touch", str(marker)])
    status, body = client.call("POST", "/api/runs/shared/resume", {})
    assert status == 400 and "entry point" in body["error"]

    # A well-formed command whose Codex binary is a script shipped inside the folder.
    evil.write_text(f"#!/bin/sh\ntouch {marker}\n")
    evil.chmod(0o755)
    settings = validate_settings({"codex_bin": str(evil)})
    run_dir = (app.runs_root / "shared").resolve()
    inputs = run_dir / "gui-inputs"
    argv = verify_argv(run_dir, settings, inputs / "question.md", inputs / "solution.md", "shared")
    _plant_job(run_dir, argv)
    status, body = client.call("POST", "/api/runs/shared/resume", {})
    assert status == 400 and "Codex binary" in body["error"]

    # Unknown flags and foreign run directories are refused as well.
    good = verify_argv(run_dir, validate_settings({}), inputs / "q.md", inputs / "s.md", "shared")
    for tampered in (
        good + ["--no-report", "--extra"],
        [a if a != str(run_dir) else str(tmp_path) for a in good],
        [a if a != str(inputs / "q.md") else "/etc/passwd" for a in good],
    ):
        _plant_job(run_dir, tampered)
        assert client.call("POST", "/api/runs/shared/resume", {})[0] == 400
    import time

    time.sleep(0.5)
    assert not marker.exists() and read_job(run_dir)["status"] == "exited"


def test_resume_rebuilds_the_entry_point_after_the_bundle_moved(tmp_path):
    from osverifier.frozen import python_module_command

    from rethlas_osv_gui.replay import ReplayError, rebuild_command
    from rethlas_osv_gui.settings import paper_argv

    run_dir = (tmp_path / "run").resolve()
    inputs = run_dir / "gui-inputs"
    settings = validate_settings({"codex_bin": "codex", "codex_home": str(tmp_path / "acct")})
    allowed = {"codex_bins": ["codex"], "codex_homes": [str(tmp_path / "acct")]}
    commands = {
        "verify": ("verify", verify_argv(run_dir, settings, inputs / "q.md", inputs / "s.md", "run")),
        "paper": ("verify", paper_argv(run_dir, settings, inputs / "paper.tex", "manual", ["thm:a", "lem:b"], "run")),
        "prove": ("prove", prove_argv(run_dir, settings, inputs / "problem.md")),
    }
    moved = "/Volumes/Rethlas-OSV/Rethlas-OSV.app/Contents/MacOS/Rethlas-OSV"
    for kind, argv in commands.values():
        assert rebuild_command({"kind": kind, "argv": argv}, run_dir, **allowed) == argv
        relocated = [moved, "--entry", *argv[2:]]
        if "--" in relocated:
            cut = relocated.index("--")
            relocated[cut + 1 : cut + 3] = [moved, "--entry"]
        assert rebuild_command({"kind": kind, "argv": relocated}, run_dir, **allowed) == argv
    # The nested verifier command of a paper run is checked like a top-level one.
    kind, argv = commands["paper"]
    cut = argv.index("--")
    nested = argv[: cut + 1] + ["/bin/sh", "-m", "os", "verify"] + argv[cut + 4 :]
    with pytest.raises(ReplayError):
        rebuild_command({"kind": kind, "argv": nested}, run_dir, **allowed)
    assert python_module_command("osverifier.cli")[0] == sys.executable
    with pytest.raises(ReplayError):  # a prove module is never used for a verify run
        rebuild_command({"kind": "verify", "argv": commands["prove"][1]}, run_dir, **allowed)
    with pytest.raises(ReplayError):  # another Codex account folder
        rebuild_command({"kind": "prove", "argv": commands["prove"][1]}, run_dir,
                        codex_bins=["codex"], codex_homes=[""])


def test_codex_status_flags_a_cli_too_old_for_current_models(tmp_path):
    old = tmp_path / "old" / "codex"
    old.parent.mkdir()
    old.write_text("#!/bin/sh\necho codex-cli 0.144.1\n")
    old.chmod(0o755)
    from rethlas_osv_gui.server import CodexTools

    status = CodexTools(tmp_path / "home").status({"codex_bin": str(old), "codex_home": None})
    assert "too old" in status["error"] and "gpt-6-astra" in status["error"]


def test_codex_status_suggests_the_account_models(tmp_path):
    from rethlas_osv_gui.server import CodexTools

    codex = tmp_path / "bin" / "codex"
    codex.parent.mkdir()
    codex.write_text("#!/bin/sh\necho codex-cli 0.162.1\n")
    codex.chmod(0o755)
    home = tmp_path / "account"
    home.mkdir()
    (home / "models_cache.json").write_text(
        '{"models": [{"slug": "gpt-6.1-sol", "visibility": "list"}, {"slug": "gpt-5.6-sol", "visibility": "list"}]}')
    status = CodexTools(tmp_path / "home").status({"codex_bin": str(codex), "codex_home": str(home)})
    assert status["models"] == ["gpt-6-astra", "gpt-6.1-sol", "gpt-5.6-sol"]
    assert status["default_model"] == "gpt-6-astra"
