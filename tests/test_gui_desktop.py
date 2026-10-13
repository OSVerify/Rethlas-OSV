"""Desktop bundle plumbing: entry-point dispatch, frozen helper commands, defaults, quit."""

from __future__ import annotations

import os
import subprocess
import sys
import threading

from conftest import Client
from osverifier import frozen
from osverifier.storage import read_json

from rethlas_osv import generator as generator_module
from rethlas_osv_gui import desktop
from rethlas_osv_gui.launcher import ENTRY_POINTS
from rethlas_osv_gui.launcher import main as launcher_main
from rethlas_osv_gui.server import App, make_server
from rethlas_osv_gui.settings import paper_argv, prove_argv, validate_settings, verify_argv


def test_module_command_switches_to_entry_flag_when_frozen(monkeypatch):
    assert frozen.python_module_command("osverifier.cli") == [
        sys.executable,
        "-m",
        "osverifier.cli",
    ]
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert frozen.is_frozen()
    command = frozen.python_module_command("osverifier.cli")
    # The bundle resolves its own executable from the OS; here that is the interpreter.
    assert os.path.samefile(command[0], sys.executable)
    assert command[1:] == ["--entry", "osverifier.cli"]
    settings = validate_settings({})
    run = desktop.Path("/tmp/x")

    def dispatches(argv, module):
        # Same file, not the same string: Linux resolves the venv link via /proc/self/exe.
        return os.path.samefile(argv[0], sys.executable) and argv[1:3] == ["--entry", module]

    assert dispatches(verify_argv(run, settings, run / "q", run / "s", "r"), "osverifier.cli")
    assert dispatches(prove_argv(run, settings, run / "p"), "rethlas_osv.cli")
    argv = paper_argv(run, settings, run / "gui-inputs/paper.tex", "smart", [], "r")
    assert dispatches(argv, "osverifier_reports.paper_prepare")
    assert dispatches(argv[argv.index("--") + 1 :], "osverifier.cli")


def test_generator_memory_server_uses_the_helper(monkeypatch):
    calls = []
    monkeypatch.setattr(
        generator_module,
        "python_module_command",
        lambda module: calls.append(module) or ["/app/OSVerify", "--entry", module],
    )

    class Transport:
        codex_bin, model, reasoning_effort = "/bin/codex", "m", "xhigh"
        connection = type("Connection", (), {"arguments": []})()

    generator = generator_module.CodexGenerator.__new__(generator_module.CodexGenerator)
    generator.transport, generator.agent_threads, generator.agent_depth = Transport(), 2, 1
    command = generator.command(desktop.Path("/ws"), desktop.Path("/att"), None, "disabled")
    assert calls == ["rethlas_osv.memory_server"]
    joined = " ".join(command)
    assert 'mcp_servers.rethlas.command="/app/OSVerify"' in joined
    assert (
        'mcp_servers.rethlas.args=["--entry", "rethlas_osv.memory_server", "--workspace", "/ws"]'
        in joined
    )


def test_launcher_dispatches_entry_points(tmp_path):
    run = tmp_path / "demo"
    completed = subprocess.run(
        [sys.executable, "-m", "rethlas_osv_gui.launcher", "--entry", "osverifier.cli", "demo", "--run-dir", str(run)],
        capture_output=True, text=True, timeout=120,
    )  # fmt: skip
    assert completed.returncode == 0, completed.stderr
    assert read_json(run / "report.json")["run_status"] == "completed"
    assert launcher_main(["--entry", "not.a.module"]) == 2
    assert launcher_main(["--entry"]) == 2
    assert {
        "osverifier.cli",
        "osverifier_reports.paper_prepare",
        "rethlas_osv.memory_server",
    } <= ENTRY_POINTS
    # Every module the GUI spawns must be dispatchable from the bundle.
    for module in ENTRY_POINTS:
        __import__(module)


def test_installation_defaults_prefer_bundled_codex(tmp_path, monkeypatch):
    bundled = tmp_path / "res" / "codex" / "codex"
    bundled.parent.mkdir(parents=True)
    bundled.write_text("#!/bin/sh\necho codex-cli 0.162.0-bundled\n")
    bundled.chmod(0o755)
    monkeypatch.setattr(desktop, "resources_dir", lambda: tmp_path / "res")
    defaults = desktop.installation_defaults(tmp_path / "home")
    assert defaults == {
        "codex_home": str(tmp_path / "home" / "codex-home"),
        "codex_bin": str(bundled),
    }
    app = App(tmp_path / "home", tmp_path / "home" / "runs", defaults=defaults, desktop=True)
    assert app.settings()["codex_bin"] == str(bundled)
    assert app.settings()["codex_home"].endswith("codex-home")
    # User choices in settings.json win over installation defaults.
    app.update_settings({"codex_bin": "/opt/other/codex"})
    assert app.settings()["codex_bin"] == "/opt/other/codex"
    assert app.settings()["codex_home"].endswith("codex-home")
    monkeypatch.setenv("OSVERIFIER_HOME", str(tmp_path / "custom"))
    assert desktop.data_dir() == tmp_path / "custom"


def test_quit_endpoint_signals_the_desktop_loop(tmp_path):
    app = App(tmp_path / "home", tmp_path / "home" / "runs", desktop=True)
    server = make_server(app, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    client = Client(server.server_address[1])
    try:
        status, state = client.call("GET", "/api/state")
        assert status == 200 and state["desktop"] is True and state["active_runs"] == []
        status, body = client.call("POST", "/api/quit")
        assert status == 200 and body["quitting"]
        assert app.quit_requested.wait(5)  # set right after the response is sent
    finally:
        server.shutdown()
        server.server_close()


def test_codex_status_creates_the_account_directory(tmp_path):
    """Codex refuses a CODEX_HOME that does not exist, so the app creates it first."""
    fake = tmp_path / "codex"
    fake.write_text("#!/bin/sh\n[ -d \"$CODEX_HOME\" ] || exit 1\necho codex-cli 0.162.0-fake\n")
    fake.chmod(0o755)
    home = tmp_path / "home"
    app = App(home, home / "runs", defaults={"codex_bin": str(fake)}, desktop=True)
    app.update_settings({"codex_home": str(home / "codex-home")})
    status = app.codex.status(app.settings())
    assert (home / "codex-home").is_dir()
    assert status["version"] == "codex-cli 0.162.0-fake" and status["error"] is None


def test_frozen_launcher_never_writes_bytecode(monkeypatch):
    """Bytecode written inside the signed bundle would invalidate its signature."""
    from types import SimpleNamespace

    from rethlas_osv_gui import launcher

    monkeypatch.setattr(sys, "dont_write_bytecode", False)
    monkeypatch.setattr(sys.modules["__main__"], "__compiled__", SimpleNamespace(), raising=False)
    assert launcher.main(["--entry", "not.a.registered.module"]) == 2
    assert sys.dont_write_bytecode is True


def test_shipped_version_matches_the_package_version():
    import tomllib
    from pathlib import Path

    import rethlas_osv

    pyproject = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert rethlas_osv.__version__ == pyproject["project"]["version"]
