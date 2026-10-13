#!/usr/bin/env bash
# Build a self-contained Linux bundle of Rethlas-OSV with Nuitka (no Python source
# shipped) and the pinned Codex CLI, as dist/linux-<arch>/Rethlas-OSV/ plus a tarball.
# The bundle runs from any folder: ./Rethlas-OSV opens the app in the browser
# (or a native window when pywebview's GTK/Qt backend is available).
set -euo pipefail
cd "$(dirname "$0")/../.."
ROOT="$PWD"
VERSION="$(uv run python -c 'import importlib.metadata as m; print(m.version("rethlas-osv"))')"
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64) TARGET=x86_64-unknown-linux-musl ;;
  aarch64|arm64) TARGET=aarch64-unknown-linux-musl; ARCH=aarch64 ;;
  *) echo "unsupported architecture $ARCH" >&2; exit 1 ;;
esac
DIST="$ROOT/dist/linux-$ARCH"
rm -rf "$DIST"
mkdir -p "$DIST"

echo "== dependencies"
uv sync --locked
uv pip install "nuitka==2.7.*" ordered-set zstandard

echo "== Codex CLI $TARGET"
bash packaging/macos/fetch-codex.sh "$TARGET" "$DIST/codex"

# The memory server loads the vendored upstream MCP source from disk at runtime, so that
# directory ships verbatim; drop any bytecode cache first so it is not copied along.
find src/rethlas_osv/vendor -name __pycache__ -type d -prune -exec rm -rf {} +
echo "== compile (this takes a while)"
uv run python -m nuitka \
  --standalone \
  --output-dir="$DIST/build" \
  --output-filename=Rethlas-OSV \
  --include-package=osverifier \
  --include-package=rethlas_osv \
  --include-package=rethlas_osv_gui \
  --include-package=osverifier_reports \
  --include-package-data=osverifier_reports \
  --include-package-data=rethlas_osv \
  --include-package=mistune \
  --include-package-data=osverifier \
  --include-package-data=rethlas_osv_gui \
  --include-raw-dir=src/rethlas_osv/vendor/generation/mcp=rethlas_osv/vendor/generation/mcp \
  --include-package=fastmcp \
  --include-package=mcp \
  --include-package=requests \
  --nofollow-import-to=pytest \
  --assume-yes-for-downloads \
  src/rethlas_osv_gui/launcher.py

APP="$DIST/Rethlas-OSV"
mv "$DIST/build/launcher.dist" "$APP"
mkdir -p "$APP/codex"
cp "$DIST/codex/codex" "$DIST/codex/codex-code-mode-host" "$DIST/codex/LICENSE-codex.txt" "$DIST/codex/VERSION" "$APP/codex/"
cp packaging/macos/THIRD-PARTY-NOTICES.md "$APP/"

echo "== archive"
tar -C "$DIST" -czf "$DIST/Rethlas-OSV-$VERSION-linux-$ARCH.tar.gz" Rethlas-OSV
echo "built $DIST/Rethlas-OSV-$VERSION-linux-$ARCH.tar.gz"
