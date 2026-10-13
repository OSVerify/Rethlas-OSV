#!/usr/bin/env bash
# Download the pinned Codex CLI release binary and its code-mode host for a target triple.
# Usage: fetch-codex.sh <target-triple> <output-dir>
#   target triples: aarch64-apple-darwin (Apple silicon), x86_64-apple-darwin (Intel),
#                   x86_64-unknown-linux-musl, aarch64-unknown-linux-musl
set -euo pipefail
CODEX_VERSION="${CODEX_VERSION:-0.162.1}"
TARGET="${1:?target triple}"
OUT="${2:?output directory}"
URL="https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/codex-${TARGET}.tar.gz"
mkdir -p "$OUT"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
echo "downloading $URL"
curl -fL --retry 3 -o "$TMP/codex.tar.gz" "$URL"
tar -xzf "$TMP/codex.tar.gz" -C "$TMP"
# The archive holds a single binary named after the target triple.
BIN="$(find "$TMP" -type f -name "codex-${TARGET}" -o -type f -name codex | head -n1)"
[ -n "$BIN" ] || { echo "no codex binary in archive" >&2; exit 1; }
install -m 0755 "$BIN" "$OUT/codex"
# Codex runs every tool call through a companion "code-mode host" that it expects next to
# its own binary; without it the model cannot read or write any file. It ships separately.
HOST_URL="https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/codex-code-mode-host-${TARGET}.tar.gz"
echo "downloading $HOST_URL"
curl -fL --retry 3 -o "$TMP/host.tar.gz" "$HOST_URL"
mkdir -p "$TMP/host"
tar -xzf "$TMP/host.tar.gz" -C "$TMP/host"
HOST="$(find "$TMP/host" -type f -name "codex-code-mode-host*" | head -n1)"
[ -n "$HOST" ] || { echo "no codex-code-mode-host binary in archive" >&2; exit 1; }
install -m 0755 "$HOST" "$OUT/codex-code-mode-host"
curl -fsSL -o "$OUT/LICENSE-codex.txt" "https://raw.githubusercontent.com/openai/codex/rust-v${CODEX_VERSION}/LICENSE"
echo "$CODEX_VERSION" > "$OUT/VERSION"
"$OUT/codex" --version || true
