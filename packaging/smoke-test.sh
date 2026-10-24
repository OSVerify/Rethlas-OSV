#!/usr/bin/env bash
# Exercise a compiled bundle without any model call:
#   1. --entry dispatch runs the offline demo review through the bundled core
#   2. the literature helper starts and exercises the bundled reference-store lock
#   3. its --supplied mode serves only a frozen user-supplied reference snapshot
#   4. the desktop server starts, reports itself, and prefers the bundled Codex
# Usage: packaging/smoke-test.sh <path-to-executable>
set -euo pipefail
BIN="${1:?path to the Rethlas-OSV executable}"
HOME_DIR="$(mktemp -d)"
trap 'rm -rf "$HOME_DIR"' EXIT

echo "== --entry osverifier.cli demo"
"$BIN" --entry osverifier.cli demo --run-dir "$HOME_DIR/demo" > /dev/null
python3 - "$HOME_DIR/demo/report.json" <<'PY'
import json, sys
report = json.load(open(sys.argv[1]))
assert report["run_status"] == "completed" and report["decision"], report
print("demo decision:", report["decision"])
PY

echo "== literature helper"
python3 - "$BIN" "$HOME_DIR" <<'PY'
import json, subprocess, sys
from pathlib import Path

root = Path(sys.argv[2])
requests = [
    {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
        "name": "resolve_reference", "arguments": {"reference": "http://127.0.0.1/blocked"}}},
]
# The private address must be rejected before HTTP; no external request or model call.
result = subprocess.run(
    [sys.argv[1], "--entry", "osverifier_reports.reference_tools",
     "--store", str(root / "references"), "--journal", str(root / "literature.jsonl")],
    input="".join(json.dumps(request) + "\n" for request in requests),
    text=True, capture_output=True, timeout=30,
)
assert result.returncode == 0, result.stderr
replies = [json.loads(line) for line in result.stdout.splitlines()]
assert len(replies) == 2, result.stdout
tools = {tool["name"]: tool for tool in replies[0]["result"]["tools"]}
assert {"search_literature", "resolve_reference", "read_reference"} <= tools.keys()
assert "text/code files" in tools["resolve_reference"]["description"]
assert not replies[1]["result"]["isError"], replies[1]
retrieval = json.loads(replies[1]["result"]["content"][0]["text"])["retrieval"]
assert retrieval["status"] == "unresolved" and "Non-public source address" in retrieval["reason"], retrieval
assert json.loads((root / "references/store-version.json").read_text()) == {"version": 5}
print("literature helper: updated tools, file lock and private-address guard passed")
PY

echo "== supplied-reference reader"
python3 - "$BIN" "$HOME_DIR/supplied" <<'PY'
import hashlib, json, subprocess, sys
from pathlib import Path

root = Path(sys.argv[2])
body = "Lemma 2. Every monotone bounded sequence converges.\n".encode()
(root / "snapshot/files").mkdir(parents=True)
(root / "snapshot/files/note.txt").write_bytes(body)
entry = {"name": "note.txt", "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body), "format": "text"}
(root / "snapshot/catalogue.json").write_text(json.dumps({"schema_version": 1, "references": [entry]}))
requests = [
    {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
        "name": "read_supplied_reference", "arguments": {"reference": "note.txt", "query": "monotone"}}},
    {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
        "name": "read_supplied_reference", "arguments": {"reference": "../snapshot/catalogue.json"}}},
]
result = subprocess.run(
    [sys.argv[1], "--entry", "osverifier_reports.reference_tools", "--supplied", str(root / "snapshot"),
     "--journal", str(root / "calls.jsonl"), "--work-dir", str(root / "reads")],
    input="".join(json.dumps(request) + "\n" for request in requests),
    text=True, capture_output=True, timeout=30,
)
assert result.returncode == 0, result.stderr
replies = [json.loads(line) for line in result.stdout.splitlines()]
assert [t["name"] for t in replies[0]["result"]["tools"]] == ["list_supplied_references", "read_supplied_reference"]
passage = json.loads(replies[1]["result"]["content"][0]["text"])
assert passage["text_match_found"] and "monotone bounded" in passage["text"], passage
assert replies[2]["result"]["isError"], replies[2]  # only catalogue names are readable
print("supplied-reference reader: tools, literal passage and name confinement passed")
PY

echo "== memory server (Rethlas MCP, minimal environment as Codex spawns it)"
python3 - "$BIN" "$HOME_DIR/workspace" <<'PY'
import json, os, subprocess, sys, time
env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/"), "PYTHONUTF8": "1"}
started = time.monotonic()
process = subprocess.Popen(
    [sys.argv[1], "--entry", "rethlas_osv.memory_server", "--workspace", sys.argv[2]],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
)
def send(message):
    process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
    process.stdin.flush()
def receive():
    line = process.stdout.readline()
    if not line.strip():
        process.stdin.close()
        try:
            code = process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            code = "still running"
        stderr = process.stderr.read().decode("utf-8", "replace")
        raise SystemExit(f"memory server sent no reply (exit {code}); its stderr:\n{stderr}")
    return json.loads(line)
send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "smoke", "version": "0"}}})
reply = receive()
elapsed = time.monotonic() - started
assert reply.get("id") == 1 and "result" in reply, reply
send({"jsonrpc": "2.0", "method": "notifications/initialized"})
send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
tools = {tool["name"] for tool in receive()["result"]["tools"]}
assert {"memory_init", "memory_append", "memory_search", "branch_update"} <= tools, tools
send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "memory_append", "arguments": {
    "problem_id": "smoke", "channel": "events", "record": {"note": "∀ε>0 ∃δ>0: non-ASCII survives"}}}})
appended = receive()
assert not appended["result"].get("isError"), appended
process.stdin.close()
assert process.wait(timeout=30) == 0, process.stderr.read().decode("utf-8", "replace")
print(f"memory server: handshake in {elapsed:.1f}s (Codex allows 120s), tools and UTF-8 records ok")
PY

echo "== desktop server"
OSVERIFIER_HOME="$HOME_DIR/data" BROWSER=/bin/true "$BIN" --no-window --port 18793 > "$HOME_DIR/desktop.log" 2>&1 &
PID=$!
for _ in $(seq 1 30); do
  if curl -fs http://127.0.0.1:18793/api/state > "$HOME_DIR/state.json" 2>/dev/null; then break; fi
  sleep 1
done
python3 - "$HOME_DIR/state.json" <<'PY'
import json, os, sys
state = json.load(open(sys.argv[1]))
assert state["desktop"] is True, state
codex_bin = state["settings"]["codex_bin"]
host = os.path.join(os.path.dirname(codex_bin), "codex-code-mode-host")
assert os.access(host, os.X_OK), f"Codex code-mode host missing next to {codex_bin}; tool calls would all fail"
print("version:", state["version"], "| codex_bin:", state["settings"]["codex_bin"], "| codex_home:", state["settings"]["codex_home"])
PY
curl -fs -X POST -H 'Content-Type: application/json' http://127.0.0.1:18793/api/quit > /dev/null
wait "$PID" || true
cat "$HOME_DIR/desktop.log"
echo "smoke test passed"
