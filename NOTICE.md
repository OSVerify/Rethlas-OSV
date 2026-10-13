Rethlas-OSV
Copyright 2026 The OSVerify Authors

Licensed under the Apache License, Version 2.0 (see LICENSE).

# Third-party attribution

This integration uses and adapts Rethlas by the contributors to
https://github.com/frenzymath/Rethlas, pinned to commit
`887cc46427636bbdd235160a112f9a30ae81d040`.

The unmodified generation-agent snapshot, upstream README, and Apache License 2.0
are retained in `src/rethlas_osv/vendor/`. `UPSTREAM.json` records
the retained file hashes. The upstream site and shell runner are not vendored.

Rethlas-OSV provides the new Python orchestration, generation transport, feedback
adapter and tests. The generation AGENTS instructions and `verify-proof` skill are
adaptations maintained outside the vendor snapshot. The MCP adapter reuses the
upstream theorem search and memory semantics (problem-id sanitization, channels,
ranking), reimplements memory file access so that it never follows workspace links,
and omits its verification tool.

The shared review policy and the proposer/validator skills of OSVerify (its
`src/osverifier/skills/`, attributed in OSVerify's own NOTICE.md) include investigation and citation checks adapted from the
upstream verification-agent skills `verify-sequential-statements` and
`check-referenced-statements` (`agents/verification/.agents/skills/` at the same
commit). Those upstream skills are not vendored or used as skills; the adapted text
is rewritten for OSVerify's issue, decision, and exact-feedback contracts.
