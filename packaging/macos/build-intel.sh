#!/usr/bin/env bash
# Build the Intel (x86_64) Rethlas-OSV.app on an Apple silicon Mac through Rosetta.
#
# The regular build script detects the architecture from the process it runs in, so
# this wrapper runs it as an x86_64 process with a separate Intel Python environment.
# Output lands in dist/macos-x86_64/. Requires Rosetta:
#   softwareupdate --install-rosetta --agree-to-license
set -euo pipefail
cd "$(dirname "$0")/../.."
PYTHON="${INTEL_PYTHON:-cpython-3.12-macos-x86_64-none}"
ENV="$PWD/.venv-x86_64"

if ! arch -x86_64 /usr/bin/true 2>/dev/null; then
  echo "Rosetta is not installed; run: softwareupdate --install-rosetta --agree-to-license" >&2
  exit 1
fi
if [ ! -x "$ENV/bin/python" ]; then
  echo "== creating the Intel Python environment in $ENV"
  uv python install "$PYTHON"
  uv venv --python "$PYTHON" "$ENV"
fi
case "$("$ENV/bin/python" -c 'import platform; print(platform.machine())')" in
  x86_64) ;;
  *) echo "$ENV is not an x86_64 environment; delete it and rerun" >&2; exit 1 ;;
esac

UV_PROJECT_ENVIRONMENT="$ENV" VIRTUAL_ENV="$ENV" arch -x86_64 ./packaging/macos/build.sh
