# Rethlas-OSV guide

Detailed reference for the GUI, the desktop app and `rethlas-osv`. Start with the [README](../README.md).

## Local GUI

`rethlas-osv-gui` starts a local web interface for people who do not want the terminal:

```bash
uv sync
uv run rethlas-osv-gui            # opens http://127.0.0.1:8765/ in the browser
uv run rethlas-osv-gui --runs-dir runs --no-browser   # browse this repository's runs
```

The page has four tabs. **Verify a paper** takes one paper (pasted or loaded from
`.tex`/`.md`/`.txt`), prepares the referee question from it, and starts
`osverify verify --review-type paper` with the full paper as the candidate solution.
Paper review completes the selected scope by default, even after a blocking finding;
the rejection-stop checkbox applies only to the advanced solution input. Three modes choose
the results to establish: **Smart** asks one fresh Codex call to select the main
theorems and the key steps they rest on; **Lazy** asks it for the results announced in
the introduction; **Manual** takes LaTeX labels and extracts those theorem-like
environments deterministically, with no model call. Every selected statement must be
a verbatim excerpt of the paper or the extraction is rejected and retried; the
prepared question, the selected targets, and the extraction record are kept in
`gui-inputs/` and shown in the run view. An advanced section still accepts a
hand-written question and solution.
**Prove a conjecture** takes a problem statement and starts `rethlas-osv`.
**Runs** lists every run directory with its status and decision, renders the audited
feedback and any handed-off proof with KaTeX, shows inputs with the line numbers the
feedback cites, tails `trajectory.jsonl`, and offers Cancel and Resume. **Settings**
edits the defaults for model, reasoning effort, web search, workers, issue cap,
attempts, timeouts, decomposition, early stop, rounds, branches, and the Codex binary
or account directory; every quick setting on the start forms maps onto the same flags.

The GUI is a front-end only, in the separate `rethlas_osv_gui` package. It launches the
existing CLIs as child processes (the exact command is stored in `gui-job.json` in the
run directory), interrupts them with SIGINT exactly like Ctrl+C, and resumes by
replaying that stored command (its arguments are validated and it is relaunched through
the current app or interpreter, never the executable named in the file), so all freezing, caching, quota, and audit behavior is
the core's. Closing the browser or the GUI does not stop a run; reopening the GUI
reattaches to it. Settings and run directories live under `~/.osverifier/` by default
(`--home`, `--runs-dir`). Users sign in with their own Codex CLI account; the GUI only
runs `codex login` and `codex login status` and stores no credentials. macOS and Linux
are supported; PDF input is not, use LaTeX or Markdown text. Only reference files
(below) may be PDFs or images.

### Reference files and restarts

**Prove a conjecture** also takes optional reference files: PDFs (scans too), images or
text that the generator and the verifier can read. The page sends the chosen files to
the GUI, which copies them into `gui-inputs/references/` in the new run folder and passes
`--references` to `rethlas-osv`. Resume therefore never depends on where the files
came from. The desktop window's file dialog cannot choose a folder, so add a folder by
selecting all of its files; names must be distinct, and 256 MB is the total limit.

When a prove run's review ends `review_inconclusive`, for example because it could not
read a cited source, the run view offers **Restart with references**. It opens the prove
form for a new run of the same problem: add the source you found, choose the branch
when the old run had several, and keep or drop the old run's own reference files. The
GUI then starts `rethlas-osv --seed-from <old run> [--seed-branch N] --references
<new run>/gui-inputs/references`; see [Restart from a draft with new
references](#restart-from-a-draft-with-new-references) for what the runner does. The old
run is only read. Resume replays `--references` only when it points inside the run,
and `--seed-from` only when it names another run folder beside this one.

### Desktop app

The product is called **Rethlas-OSV**. `packaging/macos/build.sh` compiles the GUI and the core with Nuitka into
`Rethlas-OSV.app`, bundles the pinned Codex CLI binary, and produces a `.dmg`; see
`packaging/macos/README.md` for signing and notarization. The app keeps its data in
`~/Library/Application Support/Rethlas-OSV/`, including its own Codex account
directory, so users sign in once with their own subscription and the machine's
desktop Codex account is untouched. `uv run rethlas-osv-desktop --no-window` runs the
same desktop entry point from a checkout. Inside the bundle the executable doubles
as the Python runtime for helper processes via `Rethlas-OSV --entry <module>`; any
code that spawns a module must build the command with
`osverifier.frozen.python_module_command`.

Run an existing Rethlas-style proof generator with OSVerify in one repository.
Python owns the generation/review/revision loop; Codex owns the mathematical work
inside each generation turn. The verifier core still accepts only a question and
candidate solution and does not generate replacements.

## Run end to end

Install Python 3.12+, uv, and a compatible authenticated Codex CLI. From this repository:

```bash
uv sync
uv run rethlas-osv --run-dir runs/rethlas-example
```

The included upstream example asks to prove that every finite group of prime order
is cyclic. Both models default to `gpt-6-astra` (choose others with `--generator-model` and
`--verifier-model`; `osverify models` lists them), both reasoning efforts to `xhigh`.
The verifier defaults to the parallel decomposed workflow, 16 concurrent verifier calls, no issue
cap, live search, 900 seconds per role attempt, and 2 attempts per invocation.

For your own problem:

```bash
uv run rethlas-osv \
  --problem /path/to/problem.md \
  --run-dir runs/my-problem \
  --generation-branches 4 \
  --max-rounds 10 \
  --generator-model gpt-6-astra \
  --verifier-model gpt-6.1-sol
```

Use `--codex-bin /path/to/codex` if the working CLI is not on PATH, and optionally
`--codex-home /path/to/account` to select existing CLI authentication. No login,
global skill installation, or desktop-account change is performed by this integration.
On macOS, a bundled CLI may be at `/Applications/ChatGPT.app/Contents/Resources/codex`.

Reference files in a sibling `problem.refs/` directory are included automatically,
or pass `--references /path/to/references`. PDFs require Poppler's `pdftotext` and
are extracted in the generation workspace. The same frozen files are supplied to every
review: the verifier's proposer, issue validator and feedback auditor can list and read
them (PDF pages also as images, so scans are readable), alongside live web search. This
is how a source that web search cannot retrieve, such as an old paper, can be checked.
References are not added assumptions. A supplied file is a source to compare, not proof:
the reviewers check that it is the cited work, then the cited statement, its hypotheses
and how the proof applies it. Input and reference snapshots stay in `runs/`; a file
that appears later in `inputs/references` is refused, while macOS `.DS_Store` and `._*`
files are ignored.

Useful options:

- `--generation-branches 4`: generate up to four complete candidates concurrently
  per round, in independent sessions. The default remains 1.
- `--prepare-only`: freeze inputs and prepare a workspace with zero model calls.
- `--search-policy alternating|live|disabled`: generation search; alternating retains
  Rethlas's live/independent-thinking continuation pattern. The theorem tool is absent,
  built-in web search is disabled, and the generator's shell has no network access on
  independent-thinking turns.
- `--verifier-web-search live`: independently controls verifier search.
- `--root-only`: use the legacy whole-solution verifier without decomposition.
- `--no-early-stop`: continue reviewing the remaining issues and segments after an
  audited `REJECT`, collecting their approved feedback. By default, that rejection
  stops the current candidate's review early. This option requires the decomposed
  workflow and cannot be combined with `--root-only`.
- `--generator-timeout 0`: no overall time limit for a generation turn (the default). A turn returns when the agent finishes. Quota exhaustion and Ctrl+C
  still stop it; ordinary execution errors still preserve an incomplete run.
  A positive value sets an optional limit in seconds. Verifier role timeouts remain
  separate (900 seconds by default), as do individual tool/network timeouts.
- `--agent-threads 4 --agent-depth 2`: subagent limits **inside each generation branch**;
  depth 0 disables these subagents while keeping parallel generation branches enabled.
  Subagents develop plans, while each branch's parent submits one complete candidate.
  Increasing both branch count and subagent limits increases possible concurrent work.

## Parallel candidate search

`--generation-branches N` controls search width; `--max-rounds R` controls depth per
branch. Each branch gets a separate Codex session, workspace, memory, draft, and
feedback. Branches start from the same original problem and independently choose
their approaches. Independent sessions do not guarantee distinct mathematical ideas.

In each round, Python concurrently generates one turn in every branch, snapshots
all successful outputs, then reviews every draft in branch order. Reviews
are serial; the verifier's internal issue chains still run concurrently. Only exact
approved feedback returns to the corresponding branch, which continues in its own
session next round. Partial outputs skip review. All branches finish the current
round before any advances to the next one. This is fixed-width independent search
with revision, without candidate ranking, beam pruning, or a branching search tree.

For `N=4, R=10`, the upper bound is 40 generation turns/candidate submissions.
Partial turns still consume their branch's round and may produce no full candidate.
Identical question/proof pairs share the run's review cache. The first eligible
`READY_FOR_HUMAN` review stops the experiment; other generated candidates are saved
but later reviews are skipped. The handoff records which branch and round supplied
the exact proof. An unfinished generation/review stops advancement of the whole
experiment, retaining successful siblings for manual resume.

## Decisions and stopping

The generator returns `candidate_ready` and a progress note. `false` preserves partial
work and submits it to partial review. `true` submits it to full-solution review.
Both snapshot `results/blueprint.md`; neither asserts correctness.

| Completed verifier decision | Python action |
| --- | --- |
| Partial `PASS` | Continue the same generation session; no final handoff. |
| `REVISE` | Send exact approved feedback into the same generation session. |
| `REJECT` | Send exact approved feedback and continue generation; the candidate argument must be reconsidered. |
| `READY_FOR_HUMAN` | Stop and copy the exact reviewed snapshot to `blueprint_ready_for_human.md`. |

An unfinished verification stops the invocation as `incomplete`; resume its existing
review before generating another revision. The exception is a review that finished
every task but stays inconclusive (`termination_reason=review_inconclusive`), for
example because a cited source could not be located: resuming would replay the same
cached result, so the loop records `review_inconclusive` and continues generation with
`continuation_reason=verification_inconclusive` and the exact audited feedback. Such a
review never hands off. A `REJECT` does not terminate the entire
research experiment or assert that the theorem is false. A human handoff does not
certify the proof: `human_confirmed` remains false.

`--no-early-stop` changes only rejection-based stopping inside each candidate review.
The final decision can still be `REJECT`. A `READY_FOR_HUMAN` handoff still stops
the experiment, and generation budgets, quota stops, and execution-error handling
remain in effect. The setting is frozen with the run: changing it requires a new
run directory.

The orchestrator never accepts a generator-written success marker. `handoff.json`
binds the original question, generation snapshot, completed review, and final proof
by hashes. A clean rerun validates those artifacts without new model calls.

## Resume and budgets

Repeat the same command and run directory. Finished generation turns are cached,
interrupted generation sessions are resumed by their recorded session ID, and the
verifier reuses its validated jobs. A turn that ends without a usable `results/blueprint.md`
or schema-valid final response records the reason in its attempt's
`output-rejected.json`; the resumed turn is told that reason. Identical question/proof pairs share one review
directory, preventing accidental duplicate verification charges within a run.

`--max-rounds` is the generation-turn budget **per branch**, not the number of Codex calls.
A verifier round may use many proposer/validator/auditor calls. After exhaustion,
increase `--max-rounds` to extend the same experiment. Other input, model, search,
code, branch count, or configuration changes require a fresh run directory. Partial work remains
available on exhaustion or failure.

Explicit Codex quota exhaustion is handled separately from transient failures:

- The generator or any verifier role can stop the invocation with `status=incomplete`.
  `summary.json` reports quota exhaustion, and `interruption.json` records
  `reason=quota_exhausted`, the phase, and the source error artifact.
- Python does not retry that quota failure, generate another candidate, or begin later
  verifier jobs. All active generation branches owned by this experiment, or active
  calls owned by the affected verifier backend, are stopped.
  Other independently launched experiments are not controlled by this process.
- After quota recovers, repeat the same command. The generator resumes its saved
  session; verification reuses validated jobs and reruns only unfinished jobs.
- No waiting for quota reset or automatic restart is installed. Ordinary 429 throttling,
  capacity failures, and network errors continue to use the existing retry policy.

Recognition uses explicit quota codes/messages in Codex's top-level `error` and
`turn.failed` events, with a terminal `ERROR:` stderr fallback. It does not inspect
candidate text, agent messages, or tool results for quota language. Unknown failure
formats remain ordinary execution errors. A successful resume clears the current
interruption marker while retaining attempt-level history.

### Restart from a draft with new references

References are frozen inputs: adding one to an existing run directory is refused. When
a review stays inconclusive because it could not read a cited source, and you have
since found that source, start a **new** run from the old run's latest draft:

```bash
uv run rethlas-osv --problem /path/to/problem.md --references /path/to/references \
  --seed-from runs/my-problem --run-dir runs/my-problem-with-refs
```

The problem must be identical. Round 1 of every branch reviews the old run's latest
draft with the new references, without a generation call; a `READY_FOR_HUMAN` review
hands off at once. Otherwise generation continues from round 2 in a fresh session, with
that review's exact audited feedback and the draft in `results/blueprint.md`; workspace
memory starts empty. Pass `--seed-branch N` when the old run had several branches. The
old run is only read. The new manifest records its path, manifest hash, branch, the
generation artifact and the draft's hash, and copies of both files are kept in
`inputs/seed/`. A seed cannot be added to or changed in an existing run. The seeded
round counts toward `--max-rounds` and `rounds_completed`, but not toward
`generation_turns_completed`. The GUI offers this as [Restart with references](#reference-files-and-restarts).

Quota exhaustion or Ctrl+C stops every parallel generation process. Existing runs remain
frozen to their original code/configuration: continue them with the version that started
them, or start a fresh run. SIGTERM/crash recovery remains unsupported; use Ctrl+C
for a managed manual stop. Resume uses the same command and run directory, without
a separate resume flag; completed branches are not regenerated.

Exit codes are 0 for a human handoff (or preparation only), 2 for incomplete execution,
3 for exhausted generation budget, 1 for setup/provenance failure, and 130 for interruption.

Artifacts under the selected run directory:

```text
manifest.json                    Frozen inputs, code and model/config identities
inputs/                          Original question, references and any seed draft
generator/                       Rethlas skills, experiment memory and mutable draft
rounds/0001/                     Generator prompt, command, JSONL events and snapshot
rounds/0001/feedback.json         Only exact auditor-approved feedback and status
reviews/<question-proof-hash>/   Full OSVerify artifacts and trajectory, with any
                                 supplied-references/ snapshot
trajectory.jsonl                 Outer-loop transitions
summary.json                     Status, last decision and completed rounds
interruption.json                Current quota stop and manual-resume information
handoff.json                     Hash-bound human-handoff receipt, when ready
blueprint_ready_for_human.md     Exact reviewed proof, when ready
```

With multiple generation branches, `generator/` and `rounds/` instead live under
`branches/0001/`, `branches/0002/`, etc. Shared inputs, reviews, trajectory, and handoff
stay at the run root. `summary.json` also records `generation_branches`,
`generation_turns_completed`, `candidates_generated`, and `winning_branch`.
`rounds_completed` counts fully processed waves; on a handoff it records the wave
that yielded the proof even if later candidates in that wave were not reviewed.

## How upstream was adapted

The pinned source is [Rethlas at 887cc46](https://github.com/frenzymath/Rethlas/tree/887cc46427636bbdd235160a112f9a30ae81d040).
Its generator uses strategy skills, experiment-local memory tools, retrieval, and
recursive plan exploration. Those resources are preserved as an unmodified vendored
snapshot. The original HTTP verifier and shell runner are replaced by Python control.

See [the interface analysis](rethlas-adaptation.md) for the exact upstream
input/output contracts and mapping. The adapted generation instructions and submission
skill live outside `vendor`; the runtime copies those over the upstream generation
instructions. The vendor snapshot is licensed under Apache-2.0; see
[NOTICE](../NOTICE.md) and the packaged upstream LICENSE and provenance manifest.

Tests exercise revision and rejection continuations, partial/full routing,
incomplete-review resume, exact feedback, frozen inputs, forged stop-file rejection,
handoff integrity, and the actual MCP memory-tool protocol. Scripted tests establish
plumbing only; a live example is a transport/integration check, not an open-question result.
See the [live smoke-test record](rethlas-smoke-test.md) for the exact tested
configuration, call counts, resume checks, and limits.

## Review every iteration

Every generation turn must save the entire current `results/blueprint.md`, including
explicit unfinished obligations. `candidate_ready=false` now selects partial review
rather than skipping review; `true` selects full-solution review. Partial PASS means
continue, REVISE requests corrections without guaranteeing a repair, and REJECT
requires an independently audited obstruction to the current route. Only a completed
full-solution READY_FOR_HUMAN review produces the hash-bound human handoff.

No additional CLI flag is required. The original question, independent audits,
branch-local feedback, quota stopping and checkpoint resume remain in use. Identical
drafts share caches only within the same mode. Use a new run directory after updating
the implementation; historical frozen runs are not migrated. `--root-only` remains
an explicit lower-coverage option for both partial and full reviews; the default
still includes decomposition. `--workers` caps the shared verifier pool across
root/segment proposals, decomposition, deduplication, validation, and feedback audits.
Feedback audits and validation take priority over queued proposals. Generation branch
width remains separately controlled by `--generation-branches`.

## Stalled reviews

Verifier calls in Rethlas have an operational inactivity deadline of 300 seconds
without item events. Set `--verifier-idle-timeout SECONDS`, or `0` to disable it.
Verifier calls request reasoning summaries, so a model that is still thinking keeps
producing events; a call that falls silent for this long is more likely stuck than
reasoning. The `--verifier-timeout` total deadline (900 seconds) and `--max-attempts`
bound still apply. Each retry uses a fresh call, while completed jobs retain their
caches. A call that times out earns an extra attempt (`--verifier-timeout-retries`,
default 1), and once a job has timed out its later attempts, including those after a
resume, run with doubled time limits.

If a review of either mode remains incomplete after these retries, the loop records
`review_stalled`, preserves the incomplete report and exact audited feedback, and
continues generation with `continuation_reason=verification_stalled`. No PASS or
READY_FOR_HUMAN is manufactured, existing REVISE/REJECT decisions remain intact, and
an unfinished full-solution review never hands off. A hash-bound continuation receipt
preserves the historical feedback on loop resume. If the generator resubmits the same
draft, its review resumes and retries only the unfinished tasks; a changed draft gets
a new review of the entire draft (this is not incremental verification). Quota,
cancellation and non-timeout technical failures do not qualify for this continuation
exception.
