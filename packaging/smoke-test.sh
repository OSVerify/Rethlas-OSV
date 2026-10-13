#!/usr/bin/env bash
# Exercise a compiled bundle without any model call:
#   1. --entry dispatch runs the offline demo review through the bundled core
#   2. the desktop server starts, reports itself, and prefers the bundled Codex
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
