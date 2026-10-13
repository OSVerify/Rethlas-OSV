---
name: direct-proving
description: Screen a decomposition plan by first trying to prove all of its subgoals directly, then identifying the key stuck points if the plan does not fully go through. Use when a decomposition plan is created.
---

# Direct Proving

Use this skill to screen decomposition plans by first trying to carry the whole plan through, and if it does not fully go through, then identify the key stuck points.


## Input Contract

Read:

- one decomposition plan from `subgoals`
- relevant `immediate_conclusions`, `toy_examples`, `counterexamples`, and `failed_paths`
- relevant search results and references
- any previously identified external statements whose proofs may be adaptable

## Procedure

1. Take one decomposition plan at a time.
2. For each subgoal, actively use the searched results, toy examples, and counterexamples that are most relevant to that subgoal.
3. When a similar theorem has been found, try to adapt its proof idea, construction, or reduction to the current subgoal instead of treating it as a black-box citation.
4. If that theorem is only a partial result with extra hypotheses, first analyze why the method needs those hypotheses and where it fails for the current subgoal. Do not skip this by merely trying to prove the current object satisfies the extra hypotheses and applying the partial result directly.
5. First attempt to prove all subgoals in that plan directly.
6. Try to carry the whole plan through before switching into failure diagnosis mode.
7. For each subgoal, record whether it is:
   - already solved directly
   - partially advanced
   - blocked
8. If a proof adaptation attempt fails, identify why the migration fails. Be concrete: for example, note which hypothesis is missing, which construction does not transfer, which step breaks, which counterexample blocks the migration, or which part of the searched proof depends on structure absent in the current setting.
9. If a subgoal is blocked or you get stuck while proving it, immediately try `$construct-counterexamples` for that subgoal before moving on. The goal is to test whether the subgoal itself is false, too strong, missing hypotheses, or merely hard.
10. If all subgoals are solved directly, mark the plan as solved and assemble the proof draft.
11. If the plan does not fully go through, then identify the key stuck points as concretely as possible.
12. Focus on locating the decisive failure modes of the plan after this first full attempt, not on polishing a full proof.

## Output Contract

Append one record per attempted subgoal to `proof_steps`:

```json
{
  "plan_id": "...",
  "attempt_type": "direct",
  "subgoal": "...",
  "attempt_summary": "...",
  "status": "solved|partial|stuck",
  "used_examples": ["..."],
  "used_counterexamples": ["..."],
  "key_stuck_points": ["..."],
  "used_results": ["..."],
  "adapted_from": ["relevant statements or proofs whose ideas were migrated"],
  "migration_failures": ["why a proof adaptation or migration failed"],
  "branch_id": "optional"
}
```

Update the corresponding decomposition-plan record in `subgoals` to `screening`, `screened`, or `solved`.

## MCP Tools

- `memory_search`
- `memory_append`
- `branch_update`
- `search_arxiv_theorems`

## Failure Logging

If a decomposition plan does not solve the problem directly after attempting all of its subgoals, append a `failed_paths` record that summarizes the plan-local stuck points and any important proof-migration failures.
