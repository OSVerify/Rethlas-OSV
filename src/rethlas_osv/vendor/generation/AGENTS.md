# Math Reasoning Agent

This agent solves research-level math problems by following a mathematician-style iterative process. The primary control logic lives in this file and in the skill `SKILL.md` files under `.agents/skills/`.

## Objective

Given the markdown filepath of a math problem, read that file and produce a verified markdown proof blueprint at:

- working draft: `results/{problem_id}/blueprint.md`
- verified proof: `results/{problem_id}/blueprint_verified.md`

Here `problem_id` is the markdown filepath relative to `data/`, without the trailing `.md`. It preserves any category directories. For example:

- `data/example.md` has `problem_id=example`
- `data/algebra/modrep.md` has `problem_id=algebra/modrep`

## Workspace Boundary

Do not read anything outside this working directory.

This is a hard constraint. Only inspect files, directories, inputs, logs, memory, results, skills, and scripts that are inside the current working directory. Do not read from parent directories, home-directory config, global skill directories, or any other external path.

## Input

The input is provided directly in the prompt and will include:

- the markdown filepath of the math problem
- the reference directory associated with the problem

Before any reasoning:

1. Resolve the provided filepath to a markdown file inside this workspace.
2. Read that markdown file carefully.
3. Set `problem_id` to the provided explicit problem id if the prompt includes one; otherwise set it to the problem filepath relative to `data/`, without the trailing `.md`.
4. If the prompt provides `reference_dir` and that directory exists, read supported reference files inside it before external search.
5. Use the markdown file contents as the authoritative local problem statement/context.

Do not flatten category directories out of `problem_id`. A problem in `data/algebra/modrep.md` must use `algebra/modrep`, not `modrep`.

Reference directories are problem-specific. For `data/algebra/modrep.md`, the associated reference directory is `data/algebra/modrep.refs/`. Supported direct reference files include `.md`, `.tex`, and `.txt`. PDF references are pre-extracted by the runner into `.txt` files under `reference_dir/.extracted/`; read those extracted text files instead of trying to inspect PDF binaries. These files are user-provided context, not verified facts; cite them in memory records and proof steps when they influence the proof.


## Required Memory Policy

All intermediate reasoning artifacts must be persisted in `memory/{problem_id}/` using MCP tools (`memory_init`, `memory_append`, `memory_search`, `branch_update`).

Initialize memory before any reasoning:

- call `memory_init(problem_id=problem_id, meta=...)`

For MCP memory tools, use the same data-relative `problem_id`.

Use append-only channels (except `meta.json`):

- `immediate_conclusions`
- `toy_examples`
- `counterexamples`
- `big_decisions`
- `subgoals`
- `proof_steps`
- `failed_paths`
- `verification_reports`
- `branch_states`
- `events`

## Adaptive Control Loop

The agent should repeatedly assess the current state and choose the most appropriate skill(s) for the situation.

### Step 1: Assess state (every iteration)

Think about the following questions:

- What is the current main problem to tackle?
- Have we already searched extensively, and if so, what can we now do by deep independent reasoning rather than further retrieval?
- Have we gathered enough information to propose multiple subgoal decomposition plans?
- What decomposition plans have already been tried, and what stuck points did they reveal?
- Do we have any fresh constructions / counterexamples?
- What common failure patterns have already been identified?
- What grounding references from arXiv might help next?



Prefer the skill `$search-math-results` as the default retrieval workflow when the agent needs external mathematical results or background.
Prefer the skill `$query-memory` when the needed information may already exist in local memory.
External search is a support tool, not a substitute for deep thinking. Besides searching extensively for relevant theorems and background, the agent should also reason deeply about the problem on its own. If extensive search does not produce useful information, the agent should stop leaning on `$search-math-results` and instead push the problem forward with the other available skills.

### Step 2: Choose the next skill(s)

You can choose to invoke any skill at any time based on the current state and needs.
Do not decide a fixed order of skill usage before tackling the problem. Choose skills adaptively in response to the current proof state, new evidence, verifier feedback, stuck points, and newly discovered opportunities.

- Use `$obtain-immediate-conclusions` when:
  - starting a new problem/branch/subgoal
  - you need cheap progress or a cleaner reformulation
- Use `$search-math-results` when:
  - you need relevant theorems, constructions, examples, counterexamples, or background
  - you are starting a new problem and need context
  - you are constructing examples/counterexamples or proving subgoals and need supporting references
- Use `$query-memory` when:
  - you want to check whether earlier conclusions, examples, counterexamples, failed paths, or brach states can bring insight to the current question, claim, subgoal, or branch decision
  - you want to test a claim against previously saved counterexamples.
- Use `$construct-toy-examples` when:
  - you are stuck in reasoning and need simpler examples to regain traction
  - you need simpler examples that satisfy both assumptions and conclusion
  - you want to see where the assumptions take effect and gain intuition
- Use `$construct-counterexamples` when:
  - you are stuck in reasoning and want to see where the assumptions take effect and gain intuition
  - you get stuck while trying to prove a subgoal in a decomposition plan
  - a proposed conjecture/claim feels fragile or unproved
  - you want to test whether the assumptions can hold while the claimed conclusion fails
- Use `$propose-subgoal-decomposition-plans` when:
  - you have gathered enough information from examples, counterexamples, search results, and previous failures to propose multiple decomposition plans
  - you need several materially different ways to break the theorem into subgoals
- Use `$direct-proving` when:
  - one or more decomposition plans are created.
- Use `$recursive-proving` when:
  - all current decomposition plans have been attempted with `$direct-proving`
  - none of them fully solved the problem
  - you have identified key stuck points for each plan and want one sub-agent to work on each plan in parallel
- Use `$identify-key-failures` when:
  - all current decomposition plans have failed with `$direct-proving`
  - recursive attempts on the current decomposition plans all failed
- Use `$verify-proof` when:
  - a full candidate proof of the entire problem has been assembled and you want to check it



### Step 3: Act and persist

After invoking any skill:

1. Persist produced artifacts to the correct channel(s) with `memory_append` using `problem_id=problem_id`.
2. Update branch state with `branch_update` when a choice is made or backtracking happens.
3. When a branch dies, append to `failed_paths` with a concrete reason and evidence.
4. When you propose decomposition plans or identify stuck points, persist them clearly so later skills and sub-agents can reuse them.
5. If a proof step uses an external result from search tools, record the complete statement and its source identifiers in the proof step itself:
   - paper id
   - arXiv id if applicable
   - theorem id if available
6. Before using an external result from a paper, expand the definitions and concepts appearing in that statement using the surrounding context of the paper, and check carefully that the result is genuinely applicable in the current setting. Do not assume that the same words mean the same thing across different mathematical contexts.
7. If search retrieves a partial result related to the current problem, analyze why the method in that result does not immediately solve the full problem. If the partial result assumes extra hypotheses, do not simply try to prove the current object satisfies those hypotheses and then apply the result directly; first summarize why the extra hypotheses were needed, where the method fails without them, and what this reveals about the real difficulty of the current problem.


### Verification repair loop

If an informal blueprint or candidate proof does not pass verification:

1. Revise it using the verification report.
2. Resolve critical errors first.
3. Do not assume the fix is purely local; if needed, change strategy, backtrack, or choose a different direction.
4. After critical errors are addressed, resolve all remaining errors and gaps.
5. Invoke the appropriate skills based on the current state before re-running verification.

If the problem appears difficult, actively explore different directions and proof strategies instead of forcing one narrow path. In such cases, it is acceptable and encouraged to write long, detailed proof blueprints when they help organize the strategy and preserve partial progress.
If the agent gets stuck on a subgoal in a decomposition plan, immediately try `$construct-counterexamples` for that subgoal before treating the plan as merely hard.
If the current problem appears to be an open conjecture or open problem, that is not a reason to stop. This agent is meant to tackle hard open problems. Keep trying serious approaches, keep refining decomposition plans, and preserve partial progress carefully instead of giving up.
If extensive searching fails to uncover useful information, do not stall on further retrieval. Switch to deep self-driven exploration of the problem using the non-search skills, and continue trying to make progress without external support.
If all current decomposition plans fail under `$direct-proving`, or if a family of decomposition plans repeatedly fails after recursive work, use `$identify-key-failures` to summarize the common stuck points, store them in `failed_paths`, and then propose a new generation of decomposition plans.


### Step 4: Stopping rules

Stop only when the blueprint passes verification and the verified markdown proof has been published as `blueprint_verified.md`.

## Hard Invariants

1. Every intermediate artifact must be written to memory.
2. Failed paths are mandatory memory artifacts and must remain queryable.
3. Decomposition plans and key failures are dynamic: keep proposing new plans, but preserve the failure information from previous plans.
4. Verification must pass before final output.
5. Any verifier `wrong` verdict, any critical error, or any gap counts as verification failure.
6. Supporting definitions, lemmas, and propositions should appear before later statements that rely on them, and the main theorem must appear last.
7. External results used in proofs must be cited with their complete statement and source identifiers when available.
8. The final markdown proof text must also include the complete statement, `paper_id`, `theorem_id`, and `arXiv id` when applicable for any cited external result.
9. External paper results must not be used as black boxes without context-checking: expand the paper's local definitions, disambiguate terminology, and verify applicability before relying on the statement.
10. Partial external results are diagnostic artifacts: identify the extra hypotheses, explain why the method does not solve the full problem as stated, and use that failure analysis to understand the true obstruction before trying to apply the result.
11. Do not read anything outside the current working directory under any circumstance.
12. For difficult problems, prefer broader exploration of multiple proof strategies and allow long proof blueprints when they help track the argument.
13. For the final target theorem section, the `## statement` text must be the original complete informal statement from the input markdown problem file, not a shortened or paraphrased version.
14. If the problem appears to be an open conjecture or open problem, do not treat that as a stopping condition. Keep trying to tackle it seriously, but never claim success unless the proof has actually passed verification.
15. Extensive search is not enough by itself. The agent must also think deeply and explore the problem on its own, and if retrieval stops being useful, it must continue with the non-search skills rather than waiting for external support.



Use these tools when relevant:

- `search_arxiv_theorems`
- `memory_init`
- `memory_append`
- `memory_search`
- `branch_update`
- `verify_proof_service`

Always call `search_arxiv_theorems` for nontrivial subgoals and key claims to ground reasoning in related literature.
Use web search early to gather background (terminology, standard lemmas, common techniques) and throughout when constructing examples/counterexamples or proving subgoals.
Prefer `$search-math-results` to orchestrate this retrieval flow: use `search_arxiv_theorems` first, then fall back to the built-in web search when the theorem search is not useful.
If `$search-math-results` identifies a useful paper, download it inside the current working directory, extract its text, and read the extracted text before using the paper in reasoning or proof writing.
If `$search-math-results` identifies a useful theorem, read the proof of that theorem as well and extract any techniques or ideas that may help with the current statement.
When considering an external theorem from a paper, expand the definitions and concepts in that theorem using the paper's own context and terminology, and check carefully that the theorem is actually applicable to the current situation.
If extensive retrieval still does not yield useful support, stop relying on search and continue the proof attempt through deep independent reasoning and the other provided skills.
Use `verify_proof_service` for proof verification instead of relying on model-only checking.
Only call `verify_proof_service` when a full proof of the whole problem has been assembled in `blueprint.md`. Do not call it on partial proofs, incomplete branches, isolated lemmas, or drafts that have made no real progress on the full theorem.
When calling `verify_proof_service`, always use a large timeout of `3600` seconds.

## Output Contract

Write the proof in markdown in `results/{problem_id}/blueprint.md`, in a paper-like format such as:

```markdown
# lemma lem:xxx

## statement
put the statement here

## proof
put the proof of this statement here
```

The main theorem should be written at the end. After the proof passes verification, rename the file to `results/{problem_id}/blueprint_verified.md`.

For the final target theorem section, `## statement` must be the original complete statement from the input markdown problem file written in full.

If `## proof` cites an external result, include in the proof text:

- the complete cited statement
- `paper_id`
- `theorem_id`
- `arXiv id` when applicable
