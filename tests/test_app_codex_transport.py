from __future__ import annotations

import sys

import pytest
from osverifier.backend import CodexBackend


@pytest.fixture
def fake_codex(tmp_path):
    path = tmp_path / "fake-codex"
    path.write_text(
        f"#!{sys.executable}\n"
        + """
import json, os, sys, time
from pathlib import Path
if "--version" in sys.argv:
    print("codex-cli test-fixture")
    raise SystemExit(0)
args = sys.argv
prompt = sys.stdin.read()
workspace = Path(args[args.index("--cd") + 1])
payload = json.loads((workspace / "input.json").read_text())
if payload.get("sleep"):
    time.sleep(60)
schema = json.loads(Path(args[args.index("--output-schema") + 1]).read_text())
assert schema["type"] == "object"
assert (workspace / ".agents/skills/audit-verifier-feedback/SKILL.md").is_file()
assert (workspace / ".agents/skills/policy.md").is_file()
assert "$audit-verifier-feedback" in prompt
output = {"ok": True, "selected_home": os.environ.get("CODEX_HOME")}
Path(args[args.index("--output-last-message") + 1]).write_text(json.dumps(output))
print(json.dumps({"type": "thread.started", "thread_id": "fixture-session"}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}))
"""
    )
    path.chmod(0o755)
    return path










def test_provider_reaches_subprocess_without_entering_command_artifact(tmp_path):
    home = tmp_path / "account"
    home.mkdir()
    (home / "config.toml").write_text("""model_provider = "proxy"
[model_providers.proxy]
name = "Proxy"
base_url = "http://127.0.0.1:12345/v1"
http_headers = { Authorization = "fixture-secret" }
""")
    binary = tmp_path / "codex"
    binary.write_text(
        f"#!{sys.executable}\n"
        + """
import json, pathlib, sys, tomllib
if "--version" in sys.argv:
    print("fixture")
    raise SystemExit
args = sys.argv
config = {}
for i, arg in enumerate(args):
    if arg == "-c":
        config.update(tomllib.loads(args[i + 1]))
assert config["model_provider"] == "proxy"
assert config["model_providers"]["proxy"]["http_headers"]["Authorization"] == "fixture-secret"
assert config["approval_policy"] == "never"
pathlib.Path(args[args.index("--output-last-message") + 1]).write_text('{"ok": true}')
"""
    )
    binary.chmod(0o755)
    backend = CodexBackend(model="fixture", codex_bin=str(binary), codex_home=home)
    attempt = tmp_path / "attempt"
    assert backend.generate("feedback_auditor", {}, {"type": "object"}, attempt) == {"ok": True}
    assert "fixture-secret" not in (attempt / "command.json").read_text()
    from rethlas_osv.generator import CodexGenerator

    generator = CodexGenerator(model="fixture", codex_bin=str(binary), codex_home=home)
    for session in (None, "resume-id"):
        command = generator.command(tmp_path, attempt, session, "disabled")
        assert 'model_provider="proxy"' in command
        assert "mcp_servers.rethlas.required=true" in command






STUBBORN = """
import os, signal, sys, time
from pathlib import Path
signal.signal(signal.SIGTERM, lambda *_: Path(sys.argv[1]).with_suffix(".term").touch())
Path(sys.argv[1]).touch()
time.sleep(60)
"""


def stubborn_groups(tmp_path, count):
    """Session leaders that survive SIGTERM, as a slowly exiting CLI would."""
    import subprocess
    import time

    markers = [tmp_path / f"stubborn-{index}" for index in range(count)]
    processes = [
        subprocess.Popen([sys.executable, "-c", STUBBORN, str(marker)], start_new_session=True)
        for marker in markers
    ]
    deadline = time.monotonic() + 10
    while not all(marker.exists() for marker in markers):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    return processes, markers






