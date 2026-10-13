# Rethlas integration smoke test — 2026-09-13

This is a live transport/integration check on the bundled, already-known theorem:
“Prove that every finite group of prime order is cyclic.” It is not an open-question
result or an estimate of mathematical detection quality.

## Configuration

- An OSVerify pre-release build; Codex CLI 0.153.4.
- Generation and verification: `gpt-5.6-sol`, reasoning effort `xhigh`.
- Root-first, one-layer decomposed verification; 8 issue chains, no issue cap.
- Generation round budget 2; generator subagents disabled for this elementary example.
- Generation search policy alternating (first turn live); verifier search live.
- Generator timeout 600 seconds; verifier timeout 300 seconds per role attempt.
- Pinned Rethlas source: `887cc46427636bbdd235160a112f9a30ae81d040`.

```bash
uv sync --extra rethlas
uv run rethlas-osv \
  --run-dir runs/rethlas_e2e_smoke_20260913_v2 \
  --max-rounds 2 --agent-depth 0 \
  --generator-timeout 600 --verifier-timeout 300 \
  --codex-bin /path/to/compatible/codex
```

## Observed result

| Check | Result |
| --- | --- |
| End-to-end status | `ready_for_human` after one generation round |
| Verifier decision | `READY_FOR_HUMAN`, completed, `coverage_complete=true` |
| Verifier coverage | Root and 4 verbatim segments; all 5 scopes reviewed |
| Model calls | 1 generator + 5 proposers + 1 decomposer = 7 |
| Generator MCP calls | 11 successful calls, 0 errors |
| Findings / released feedback | None |
| Human confirmation | `false` |
| Terminal rerun | Same result, zero new model calls |

A separate, one-call live transport check resumed the recorded generation session
with search disabled. The agent successfully queried the existing local memory,
returned the same session ID, and left the proof file byte-identical. Its artifacts
are under `runs/rethlas_resume_transport_20260913`.

The final proof SHA-256 is
`045acc92fd451f9885e3a3761e37a1260302eed46b98e136a7a04df7558399de`.
The experiment's package-code fingerprint is
`9908abc3e15b5ef3223c827bb0ba4780b87217e0e3d367ca189929658ffd024c`.
The local run retains the manifest, generation snapshot, complete review trajectory,
role traces, exact reviewed proof, and hash-bound `handoff.json`. Raw run directories
are gitignored; this committed record summarizes the observed results.

An earlier smoke run exposed blocked MCP tool calls under noninteractive approval
settings. The final version explicitly approves only the locally registered Rethlas
server's tools. The corrected fresh run above exercised those calls successfully.

## Other validation and limits

All 78 tests passed, including 11 Rethlas integration test instances. They cover
`REVISE` and `REJECT` continuation, exact approved feedback, partial-progress gating,
incomplete-review resume without regeneration, frozen inputs, fake success files,
handoff integrity, command construction, and the actual FastMCP memory protocol.
Ruff passed. The wheel built successfully and contains all 64 package source/assets
byte-for-byte; all 31 retained upstream files match the pinned provenance hashes.

The live example raised no issues, so it did not invoke an issue validator or feedback
auditor, or require mathematical revision. Those paths are covered by scripted tests
and the verifier's separate existing experiments, not demonstrated by this live run.
Recursive generator subagents and theorem retrieval were not exercised in this smoke
test. No open question or human-effort comparison was attempted.
