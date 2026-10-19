# macOS app build

## Without a Mac: GitHub Actions

The workflow `.github/workflows/desktop-apps.yml` builds the app on GitHub's macOS
runners for Apple silicon and Intel, plus a Linux bundle on Ubuntu. Open the
repository's Actions tab, run "Desktop apps", and download the `Rethlas-OSV-arm64` /
`Rethlas-OSV-x86_64` artifacts (each holds the `.dmg`) or `Rethlas-OSV-linux-x86_64`.
Each job runs `packaging/smoke-test.sh` against its compiled binary, so a bundling
problem fails the job instead of reaching a user. Pushing a tag such as `v0.6.0` runs it too. Add the
secrets named at the top of the workflow to get a signed and notarized build;
without them the build is ad-hoc signed. macOS runner minutes are billed at a
higher rate on private repositories.

## On a Mac

Build on a Mac with Xcode command line tools and [uv](https://docs.astral.sh/uv/):

```bash
uv venv --python 3.12   # first time only: match the CI workflow's Python
./packaging/macos/build.sh
```

This compiles the Python code with Nuitka (no `.py` source ships), downloads the
pinned Codex CLI binary and its `codex-code-mode-host` companion (Codex runs every
tool call through it) for the Mac's architecture, and produces
`dist/macos-<arch>/Rethlas-OSV.app` and a `.dmg`. The app is not universal: Apple
silicon users need the `arm64` image and Intel users the `x86_64` image.

### Intel build on an Apple silicon Mac

Rosetta lets the same Mac build the Intel app. Install Rosetta once with
`softwareupdate --install-rosetta --agree-to-license`, then run:

```bash
./packaging/macos/build-intel.sh
```

The wrapper installs an Intel Python 3.12 through uv into `.venv-x86_64`, runs the
regular build script as an x86_64 process, and writes `dist/macos-x86_64/`. The
compile takes roughly eight minutes. `pyproject.toml` pins `cryptography<49` for Intel
macOS only, because newer releases publish no Intel wheels and a source build there
cannot import; without the pin the Rethlas memory server fails inside the Intel app.

For distribution outside your own Macs you need an Apple Developer ID:

```bash
export SIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)"
xcrun notarytool store-credentials osverifier --apple-id you@example.com --team-id TEAMID
NOTARY_PROFILE=osverifier ./packaging/macos/build.sh
```

Without a signing identity the app is ad-hoc signed and Gatekeeper makes users
right-click and choose Open on first launch.

## What the app does at runtime

- Data lives in `~/Library/Application Support/Rethlas-OSV/` (settings, `runs/`, and
  `codex-home/`, the app's own Codex account directory). The machine's desktop Codex
  account is never read or changed.
- Settings default to the bundled Codex CLI; users press **Sign in** once and log in
  with their own subscription in the browser.
- The app's executable also serves as the runtime for its helper processes through
  `Rethlas-OSV --entry <module>`, so no Python installation is needed.
- If the native window cannot open, the app falls back to the default browser and
  offers a **Quit** button in the page header.

## First build on a Mac

Verified on an Apple silicon Mac (macOS 26, uv-managed Python 3.12, Nuitka 4.2): the
build takes about five minutes and yields a 411 MB app plus a 166 MB `.dmg`, and
`packaging/smoke-test.sh` passes against it. Two things mattered: Nuitka's own
pywebview plugin picks the Cocoa backend, so `webview` must not be force-included, and
the launcher must use absolute imports because Nuitka compiles it as a top-level
script. The app icon is `packaging/macos/icon/rethlas-osv.icns`, rendered from the
`rethlas-osv.svg` beside it (the same mark the page header uses). The build excludes
`tkinter`: nothing in the app uses it (pyperclip only names it as a clipboard fallback),
and uv-managed Pythons link it against Tcl/Tk 9 through `@rpath`, which Nuitka's
dependency scan cannot resolve. The smoke test's memory-server step is the one that
catches a bundle whose Rethlas MCP server cannot start. Installing
`ccache` speeds up rebuilds.
