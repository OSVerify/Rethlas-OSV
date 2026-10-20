#!/usr/bin/env bash
# Build Rethlas-OSV.app on macOS with Nuitka (compiled, no Python source shipped),
# bundle the Codex CLI and its code-mode host, then sign and optionally notarize.
#
#   ./packaging/macos/build.sh                 # build for this Mac's architecture
#   SIGN_IDENTITY="Developer ID Application: Name (TEAMID)" ./packaging/macos/build.sh
#   NOTARY_PROFILE=rethlas-osv ./packaging/macos/build.sh   # also notarize + staple
#
# Requirements on the build Mac: Xcode command line tools, uv, an internet connection.
set -euo pipefail
cd "$(dirname "$0")/../.."
ROOT="$PWD"
VERSION="$(uv run python -c 'import importlib.metadata as m; print(m.version("rethlas-osv"))')"
ARCH="$(uname -m)"
case "$ARCH" in
  arm64) TARGET=aarch64-apple-darwin ;;
  x86_64) TARGET=x86_64-apple-darwin ;;
  *) echo "unsupported architecture $ARCH" >&2; exit 1 ;;
esac
DIST="$ROOT/dist/macos-$ARCH"
rm -rf "$DIST"
mkdir -p "$DIST"

echo "== dependencies"
uv sync --locked --extra desktop
uv pip install "nuitka==2.7.*" ordered-set zstandard

echo "== Codex CLI $TARGET"
bash packaging/macos/fetch-codex.sh "$TARGET" "$DIST/codex"

# The memory server loads the vendored upstream MCP source from disk at runtime, so that
# directory ships verbatim; drop any bytecode cache first so it is not copied along.
find src/rethlas_osv/vendor -name __pycache__ -type d -prune -exec rm -rf {} +
echo "== compile (this takes a while)"
uv run python -m nuitka \
  --standalone \
  --macos-create-app-bundle \
  --macos-app-mode=gui \
  --macos-app-name=Rethlas-OSV \
  --macos-app-icon=packaging/macos/icon/rethlas-osv.icns \
  --macos-app-version="$VERSION" \
  --macos-signed-app-name=org.rethlas.osv \
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
  --include-package=objc \
  --include-package=Foundation \
  --include-package=AppKit \
  --include-package=WebKit \
  --include-package=UniformTypeIdentifiers \
  --nofollow-import-to=pytest \
  --nofollow-import-to=tkinter \
  --nofollow-import-to=_tkinter \
  --assume-yes-for-downloads \
  src/rethlas_osv_gui/launcher.py

APP="$DIST/build/launcher.app"
[ -d "$APP" ] || APP="$(find "$DIST/build" -maxdepth 1 -name '*.app' | head -n1)"
mv "$APP" "$DIST/Rethlas-OSV.app"
APP="$DIST/Rethlas-OSV.app"

echo "== resources"
mkdir -p "$APP/Contents/Resources/codex"
cp "$DIST/codex/codex" "$DIST/codex/codex-code-mode-host" "$DIST/codex/LICENSE-codex.txt" "$DIST/codex/VERSION" "$APP/Contents/Resources/codex/"
cp packaging/macos/THIRD-PARTY-NOTICES.md "$APP/Contents/Resources/"
/usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier org.rethlas.osv" "$APP/Contents/Info.plist" || true
/usr/libexec/PlistBuddy -c "Add :LSMinimumSystemVersion string 12.0" "$APP/Contents/Info.plist" || true

echo "== sign"
if [ -n "${SIGN_IDENTITY:-}" ]; then
  for helper in codex codex-code-mode-host; do
    codesign --force --options runtime --timestamp --sign "$SIGN_IDENTITY" "$APP/Contents/Resources/codex/$helper"
  done
  codesign --force --deep --options runtime --timestamp \
    --entitlements packaging/macos/entitlements.plist --sign "$SIGN_IDENTITY" "$APP"
  codesign --verify --deep --strict "$APP"
else
  codesign --force --deep --sign - "$APP"
  echo "ad-hoc signed only: Gatekeeper will warn on other Macs; set SIGN_IDENTITY for release builds"
fi

echo "== disk image"
DMG="$DIST/Rethlas-OSV-$VERSION-$ARCH.dmg"
STAGE="$DIST/dmg"
rm -rf "$STAGE"; mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname Rethlas-OSV -srcfolder "$STAGE" -ov -format UDZO "$DMG"

if [ -n "${NOTARY_PROFILE:-}" ]; then
  echo "== notarize"
  xcrun notarytool submit "$DMG" --keychain-profile "$NOTARY_PROFILE" --wait
  xcrun stapler staple "$APP"
  xcrun stapler staple "$DMG"
fi
echo "built $DMG"
