"""Desktop app: the local server plus a native window (or the default browser)."""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import sys
import threading
import webbrowser
from pathlib import Path

from osverifier.frozen import is_frozen

from .server import App, make_server

APP_NAME = "Rethlas-OSV"


def data_dir() -> Path:
    """Per-user application data: settings, runs, and the app's own Codex account."""
    override = os.environ.get("OSVERIFIER_HOME")
    if override:
        return Path(override).expanduser()
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / "rethlas-osv"


def resources_dir() -> Path:
    """Where the bundle keeps non-Python resources such as the Codex binary."""
    if getattr(sys, "_MEIPASS", None):  # PyInstaller
        return Path(sys._MEIPASS)
    executable = Path(sys.executable).resolve()
    if is_frozen():
        # macOS app bundle: Contents/MacOS/<exe> -> Contents/Resources
        if executable.parent.name == "MacOS" and (executable.parents[1] / "Resources").is_dir():
            return executable.parents[1] / "Resources"
        return executable.parent
    return Path(__file__).parent


def bundled_codex() -> Path | None:
    for candidate in (resources_dir() / "codex" / "codex", resources_dir() / "codex"):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def installation_defaults(home: Path) -> dict:
    """Prefer the bundled Codex CLI and keep its account inside the app's data folder.

    Users sign in with their own subscription from the Settings tab; the desktop
    Codex account of the machine is never read or changed.
    """
    codex = bundled_codex()
    if codex is None and shutil.which("codex") is None:
        # Finder launches apps with a minimal PATH; look in the usual places.
        for candidate in (
            "/opt/homebrew/bin/codex",
            "/usr/local/bin/codex",
            Path.home() / ".local/bin/codex",
        ):
            if Path(candidate).is_file():
                codex = Path(candidate)
                break
    defaults = {"codex_home": str(home / "codex-home")}
    if codex is not None:
        defaults["codex_bin"] = str(codex)
    return defaults


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Rethlas-OSV desktop app")
    p.add_argument(
        "--home", type=Path, default=None, help="Data folder (default: per-user app data)"
    )
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--no-window", action="store_true", help="Open the default browser instead")
    return p


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    home = (args.home or data_dir()).expanduser().resolve()
    app = App(home, home / "runs", defaults=installation_defaults(home), desktop=True)
    server = make_server(app, "127.0.0.1", args.port)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="http")
    thread.start()
    print(f"{APP_NAME}: {url}  (data: {home})", flush=True)

    def watch_quit():
        app.quit_requested.wait()
        server.shutdown()

    threading.Thread(target=watch_quit, daemon=True, name="quit").start()
    try:
        if not args.no_window:
            try:
                import webview  # pywebview: native window, optional at runtime
            except ImportError:
                webview = None
            if webview is not None:
                webview.settings["ALLOW_DOWNLOADS"] = True
                webview.create_window(APP_NAME, url, width=1180, height=860, min_size=(720, 500))
                webview.start()
                return 0
        webbrowser.open(url)
        while thread.is_alive() and not app.quit_requested.is_set():
            app.quit_requested.wait(1.0)
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        server.shutdown() if not app.quit_requested.is_set() else None
        server.server_close()
        for path in app.jobs.shutdown_note():
            print(f"still running on its own: {path}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
