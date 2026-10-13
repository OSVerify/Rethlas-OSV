# Rethlas-OSV

This repository holds the Rethlas generation integration (`src/rethlas_osv`) and the
GUI/desktop app (`src/rethlas_osv_gui`). The verifier is the separately installed
`osverifier` package from OSVerify/OSVerify; its AGENTS.md defines the review contracts
(decisions, exact audited feedback, quota handling, provenance). Never weaken them here,
never use core private attributes, and never copy or patch core code into this repository.

Use `uv sync --extra desktop`, `uv run pytest`, and `uv run ruff check .`. OSVerify is
pinned to a release tag in `[tool.uv.sources]`; to change both together, point it at a
local checkout temporarily and pin the new tag before merging.

Transport must inherit only selected user API/provider/auth configuration, never the
whole user config. Apply the same transport snapshot to verifier and generator; redact
connection overrides in persisted commands and preserve their fingerprint on resume.

The Rethlas vendor directory is an attributable upstream snapshot. Keep it unmodified;
maintain runtime adaptations outside it. Integration success means a hash-bound
READY_FOR_HUMAN artifact, never mathematical certification. Only exact audited
feedback may cross from the verifier into the generation session. The integration's
memory is experiment-local and unrelated to Codex's personal memory directory.

Generator isolation is instruction-level only: the workspace-write sandbox can read
the run directory, including review records. State this limitation; never describe the
isolation as enforced.

Parallel generation uses independent branch workspaces, memory, sessions, and exact
feedback. Width is frozen; max_rounds is depth per branch and may increase on resume.
Finish a generation wave before advancing any branch. Review candidates serially,
with a fresh Codex verifier backend per candidate so rejection cancellation cannot
poison later reviews. Share exact-input review caches; stop on the first eligible
human handoff. Quota exhaustion or Ctrl+C must stop all owned generation process
groups while retaining successful snapshots and interrupted sessions for manual resume.
Generation turns have no wall-clock deadline by default (`--generator-timeout 0`);
quota detection must keep polling, and manual cancellation must remain effective.
Explicit positive generation deadlines and separate verifier/tool timeouts remain supported.

The GUI lives in the separate `src/rethlas_osv_gui` package and must stay outside
`src/rethlas_osv`, because the Rethlas runner fingerprints that package (and the
installed OSVerify core) into every experiment manifest. It launches the CLIs as child processes, cancels with SIGINT, and resumes
by replaying the stored command's validated arguments through the current entry point
(`rethlas_osv_gui/replay.py`; never the stored executable); it never calls the verifier
in-process or bypasses the CLI contracts. Only LaTeX/Markdown text input; no Windows support; users bring their
own Codex account.
Helper processes (verifier, prover, memory server, paper extraction) must be spawned
through `osverifier.frozen.python_module_command`, never a literal `python -m`, so the
compiled desktop bundle can dispatch them with `--entry`. The product name is Rethlas-OSV; macOS is the first desktop
target; users sign in with their own Codex subscription inside the app.
