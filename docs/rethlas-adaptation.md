# Rethlas interface review and OSVerify adaptation

Source reviewed: [frenzymath/Rethlas, commit 887cc46427636bbdd235160a112f9a30ae81d040](https://github.com/frenzymath/Rethlas/tree/887cc46427636bbdd235160a112f9a30ae81d040).
The generation AGENTS instructions, all verification boundaries, generator MCP
client, verification HTTP server, output schema/writer, and shell continuation loop
were read before implementing the adapter.

## Upstream flow and contracts

1. `agents/generation/tests/run_example.sh` starts `codex exec` and resumes one session
   for a bounded number of iterations. Continuations alternate disabled/live search.
2. `agents/generation/AGENTS.md` and its strategy skills direct mathematical work:
   retrieval, local memory, examples/counterexamples, decomposition, direct attempts,
   recursive subagents, failure analysis, and a full Markdown blueprint.
3. `agents/generation/.agents/skills/verify-proof/SKILL.md` calls the generator MCP
   tool `verify_proof_service` only for a full proof. It supplies two strings:
   `statement` and `proof` (the blueprint's raw Markdown).
4. `agents/generation/mcp/server.py` posts those strings to `/verify` at the local
   verification HTTP service, by default with a 3,600-second timeout.
5. `agents/verification/api/server.py` starts another Codex agent, which sequentially
   checks proof statements and external references under its own skills. It writes
   a JSON report, read back by the HTTP service.
6. `agents/verification/mcp/server.py` validates the output schema and the verdict
   consistency rules when writing the output. The report has `verification_report`
   (`summary`, `critical_errors`, `gaps`), `verdict` (`correct` or `wrong`), and
   `repair_hints`. Any nonempty error/gap collection requires `wrong`; a clean report
   requires `correct` and empty repair hints.
7. The generator repairs failed proofs. A passing proof is renamed
   `blueprint_verified.md`; the shell runner stops when that file exists.

## Changes and reasons

The user authorized reimplementing orchestration. Retaining the original binary
verdict contract would collapse OSVerify's revision/rejection distinction and
would overstate the meaning of a human handoff. A direct Python boundary also removes
the long-lived verification HTTP request and permits deterministic snapshot/resume checks.

| Upstream concept | New integration |
| --- | --- |
| Problem markdown / `statement` | Immutable `ReviewRequest.question`, always the original input, independently of any generator-written statement |
| Full blueprint / `proof` | Frozen `ReviewRequest.candidate_solution` copied from the submitted draft |
| Agent decides when to invoke verifier | Structured `candidate_ready` submission at the end of a generator turn; Python invokes verification |
| Single verification-agent run | OSVerify root/segment proposer, issue validator, exact-feedback auditor, and audited deduplication |
| Binary `wrong` | Native `REVISE` or `REJECT`; both lead to a new generation turn with exact audited feedback |
| Binary `correct` | Native `READY_FOR_HUMAN`; never relabeled mathematical correctness |
| Repair hints | Only `ReleasedFeedback.text` and approved metadata, copied without an LLM rewrite |
| Verification execution failure | `incomplete`; restore the same review before continuing generation |
| Generator writes stop file | Python writes and validates a hash-bound receipt and exact reviewed proof |
| Shell text parsing for session ID | JSONL `thread.started` events and checked resume IDs |

The verifier still implements only full-solution mode. The generator may preserve
partial progress between turns, but that is not partial-solution verification.
An audited rejection is final for that submitted argument; the generator may revise
or choose another strategy in the next turn. Internal rationales, withheld findings,
and unapproved repair suggestions are never included in the feedback adapter.

Rethlas's ten generation skills and its memory/retrieval semantics are retained.
The runtime replaces its AGENTS instructions and `verify-proof` skill to match the
Python submission boundary. The memory server reuses upstream problem-id sanitization,
channels, and ranking, but performs its own file access without following links,
because it runs outside the generator's sandbox. It registers four experiment-memory tools and, on
search-enabled turns, the original theorem-search tool. As in upstream's MCP wrapper,
the model supplies only the query and result count, not the endpoint or timeout. There is no HTTP verification
service or `verify_proof_service` tool in this reimplementation, and no upstream
`correct`/`wrong` compatibility field is fabricated.

## Operational provenance

The source snapshot and license are packaged alongside the integration, so a cloned
repository or installed wheel does not need a separate Rethlas checkout. Its exact
commit and retained file hashes are in `vendor/UPSTREAM.json`. Each experiment freezes
inputs, reference hashes, generator/verifier configuration, and implementation hashes.
Historical verifier runs remain local and unmodified; the old local directory name
can remain as a symlink after the repository rename.

Generation uses a workspace-write sandbox and a workspace-local MCP configuration;
verification retains its read-only sandbox. When the orchestrator restores instructions,
references, or drafts in a generation workspace, it replaces links the generator may
have created there instead of writing through them. Account authentication is reused without
copying auth files or changing the desktop account. Rethlas search can fetch public
references; user-supplied research inputs and run logs are not committed.
The locally registered Rethlas MCP tools have explicit `approve` defaults, so the
noninteractive `never` approval policy does not block experiment-memory calls. This
setting is scoped to that server in the subprocess configuration.

Codex transport follows the official [non-interactive interface](https://learn.chatgpt.com/docs/non-interactive-mode)
and [MCP configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=cli), checked
against the installed CLI's `exec` and `exec resume` help. CLI/settings compatibility
and mathematical quality are verified separately.
