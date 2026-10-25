"""Resumable generation -> audited review -> revision; no automatic proof certificate."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from osverifier.backend import DEFAULT_MODEL, BackendCancelled, CodexBackend, deferred_sigint
from osverifier.models import ReviewRequest
from osverifier.quota import RESUME_MESSAGE, QuotaExhausted, record_quota
from osverifier.storage import (
    FrozenRunMismatch,
    atomic_json,
    atomic_text,
    digest,
    read_json,
    run_lock,
)
from osverifier.supplied_references import finder_metadata

from . import workspace as workspace_files
from .contracts import (
    ExperimentResult,
    GenerationTurn,
    InvalidGenerationOutput,
    generator_feedback,
    inconclusive_feedback,
    read_report,
    stalled_review_feedback,
)

ROOT = Path(__file__).parent


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def error_text(exc):
    """Exception text for persisted artifacts. Subprocess errors carry their argv, which
    can include connection overrides, so only their outcome is kept."""
    if isinstance(exc, subprocess.TimeoutExpired):
        return f"no completion within {exc.timeout} seconds"
    if isinstance(exc, subprocess.CalledProcessError):
        return f"exited with status {exc.returncode}"
    return str(exc)


def code_fingerprint():
    """This integration plus the OSVerify core it drives (separately installed)."""
    import osverifier

    roots = {"rethlas_osv": ROOT, "osverifier": Path(osverifier.__file__).parent}
    return digest(
        {
            f"{name}/{p.relative_to(root)}": sha(p)
            for name, root in roots.items()
            for p in sorted(root.rglob("*"))
            if p.is_file() and "__pycache__" not in p.parts
            and p.suffix in (".py", ".md", ".toml", ".yaml", ".json")
        }
    )


@dataclass(frozen=True)
class LoopConfig:
    max_rounds: int = 10
    search_policy: str = "alternating"
    generation_branches: int = 1

    def __post_init__(self):
        if self.max_rounds < 1 or self.search_policy not in ("alternating", "live", "disabled"):
            raise ValueError("invalid round budget or generation search policy")
        if self.generation_branches < 1:
            raise ValueError("generation_branches must be positive")


class Experiment:
    def __init__(self, generator, verifier, config: LoopConfig | None = None, event_sink=None):
        self.generator, self.verifier = generator, verifier
        self.config = config or LoopConfig()
        self.event_sink = event_sink
        self._event_lock = threading.Lock()

    def emit(self, directory, event, **fields):
        row = {"at": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
        with self._event_lock:
            with (directory / "trajectory.jsonl").open("a", encoding="utf-8") as out:
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
            if self.event_sink:
                self.event_sink(row)

    def run(
        self,
        problem: Path,
        directory: Path,
        references: Path | None = None,
        *,
        prepare_only=False,
        seed_from: Path | None = None,
        seed_branch: int | None = None,
    ):
        """``seed_from`` starts a new run from another run's latest draft (see ``seed_record``)."""
        directory = directory.expanduser().resolve()
        with run_lock(directory):
            return self._run(
                problem, directory, references, prepare_only=prepare_only,
                seed_from=seed_from, seed_branch=seed_branch,
            )

    def _run(
        self, problem, directory, references, *, prepare_only=False, seed_from=None, seed_branch=None
    ):
        try:
            question = problem.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(
                f"problem file {problem} is not UTF-8 text (byte {exc.start}: {exc.reason}); "
                "save it as UTF-8"
            ) from None
        if not question.strip():
            raise ValueError("problem must be nonblank")
        ref_files = {}
        if references is not None:
            if not references.is_dir():
                raise ValueError("reference directory does not exist")
            for p in sorted(references.rglob("*")):
                if finder_metadata(p):
                    continue
                if p.is_symlink():
                    raise ValueError("reference snapshots must not contain symlinks")
                if p.is_file():
                    ref_files[str(p.relative_to(references))] = sha(p)
        seed = self.seed_record(directory, question, seed_from, seed_branch)
        frozen = {
            "question": question,
            "references": ref_files,
            "generator": self.generator.identity,
            "verifier": {
                "backend": self.verifier.backend.identity,
                "config": asdict(self.verifier.config),
                "class": type(self.verifier).__name__,
            },
            "search_policy": self.config.search_policy,
            "generation_branches": self.config.generation_branches,
            "code_sha256": code_fingerprint(),
        }
        if seed is not None:
            frozen["seed"] = seed
        manifest = {"fingerprint": digest(frozen), "provenance": frozen}
        if (directory / "manifest.json").exists():
            if read_json(directory / "manifest.json") != manifest:
                raise FrozenRunMismatch(
                    "problem, references, code, backend or search policy changed; use a new run directory"
                )
        else:
            if seed is not None:
                # Copied before the manifest exists, so an interrupted start can simply rerun.
                parent = Path(seed["run_dir"])
                (directory / "inputs/seed").mkdir(parents=True, exist_ok=True)
                shutil.copyfile(parent / "manifest.json", directory / "inputs/seed/manifest.json")
                shutil.copyfile(parent / seed["generation_artifact"], directory / "inputs/seed/generation.json")
            atomic_json(directory / "manifest.json", manifest)
            atomic_text(directory / "inputs/problem.md", question)
            for rel in ref_files:
                target = directory / "inputs/references" / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(references / rel, target)
        if (directory / "inputs/problem.md").read_text(encoding="utf-8") != question:
            raise FrozenRunMismatch("frozen problem snapshot changed")
        for rel, expected in ref_files.items():
            if sha(directory / "inputs/references" / rel) != expected:
                raise FrozenRunMismatch("frozen reference snapshot changed")
        # Reviews read the whole directory, so a file the manifest never recorded must not appear.
        stored = directory / "inputs/references"
        present = {str(p.relative_to(stored)) for p in stored.rglob("*") if not p.is_dir() and not finder_metadata(p)}
        if present != set(ref_files):
            raise FrozenRunMismatch("frozen reference snapshot changed")
        if seed is not None and (
            sha(directory / "inputs/seed/manifest.json") != seed["manifest_sha256"]
            or sha(directory / "inputs/seed/generation.json") != seed["generation_sha256"]
        ):
            raise FrozenRunMismatch("frozen seed snapshot changed")
        # The verifier reads the same frozen files as the generator, with its own provenance.
        review_references = directory / "inputs/references" if ref_files else None
        if (directory / "handoff.json").exists():
            return self.check_handoff(directory)
        (directory / "interruption.json").unlink(missing_ok=True)
        self.emit(
            directory,
            "invocation",
            max_rounds=self.config.max_rounds,
            generation_branches=self.config.generation_branches,
        )
        branches = {
            branch: {
                "directory": self.branch_directory(directory, branch),
                "session_id": None,
                "feedback": None,
            }
            for branch in range(1, self.config.generation_branches + 1)
        }
        if seed is not None:
            self.plant_seed(directory, branches, seed)
        state = ExperimentResult(
            status="running",
            rounds_completed=0,
            generation_branches=self.config.generation_branches,
        )

        def save():
            # Count persisted successful generation turns, including pending reviews.
            # A seeded draft was generated by another run, so it is not counted here.
            artifacts = [
                read_json(path)
                for branch in branches.values()
                for path in branch["directory"].glob("rounds/*/generation.json")
            ]
            generated = [saved for saved in artifacts if "seed" not in saved]
            state.generation_turns_completed = len(generated)
            state.candidates_generated = sum(
                saved["output"]["turn"]["candidate_ready"] for saved in generated
            )
            atomic_json(directory / "summary.json", state.model_dump(mode="json"))
            return state

        save()
        phase = "generation"
        try:
            for branch in branches.values():
                self.prepare_workspace(
                    branch["directory"] / "generator", directory, question, frozen
                )
            if prepare_only:
                self.emit(directory, "prepared", note="No model calls; not an evaluation result.")
                return save()
            for index in range(self.config.max_rounds):
                if code_fingerprint() != frozen["code_sha256"]:
                    raise FrozenRunMismatch("implementation changed during experiment")
                search = self.config.search_policy
                if search == "alternating":
                    search = "live" if index % 2 == 0 else "disabled"
                phase = "generation"
                self.emit(
                    directory, "generation_wave_started", round=index + 1, branches=len(branches)
                )
                generated = self.generate_wave(directory, branches, index, search, question)
                save()
                if code_fingerprint() != frozen["code_sha256"]:
                    raise FrozenRunMismatch("implementation changed during experiment")
                # All branches finish this generation wave before any branch deepens.
                # Reviews are serial; each review retains its own internal concurrency.
                for branch_id, branch in branches.items():
                    output = generated[branch_id]
                    branch["session_id"] = output["session_id"]
                    mode = "full_solution" if output["turn"]["candidate_ready"] else "partial"
                    candidate = output["candidate_solution"]
                    turn_dir = branch["directory"] / "rounds" / f"{index + 1:04d}"
                    key = digest({"question": question, "candidate_solution": candidate, "mode": mode})
                    review_dir = directory / "reviews" / key
                    request = ReviewRequest(
                        request_id=f"candidate-{key}",
                        question=question,
                        candidate_solution=candidate,
                        mode=mode,
                    )
                    self.emit(
                        directory,
                        "verification_started",
                        round=index + 1,
                        branch=branch_id,
                        candidate_sha256=key,
                        mode=mode,
                    )
                    phase = "verification"
                    continuation = turn_dir / "stalled-continuation.json"
                    if continuation.exists():
                        receipt = read_json(continuation)
                        if receipt["report_sha256"] != sha(turn_dir / "stalled-report.json") or receipt["feedback_sha256"] != sha(turn_dir / "feedback.json"):
                            raise FrozenRunMismatch("stalled continuation artifacts changed")
                        report = read_report(read_json(turn_dir / "stalled-report.json"))
                    else:
                        report = self.verify_candidate(request, review_dir, review_references)
                    self.emit(
                        directory,
                        "verification_finished",
                        round=index + 1,
                        branch=branch_id,
                        decision=report.decision,
                        run_status=report.run_status,
                    )
                    if report.mode != mode or report.request_id != request.request_id:
                        raise FrozenRunMismatch("verifier returned a different request or mode")
                    state.last_decision = report.decision
                    stalled_feedback = read_json(turn_dir / "feedback.json") if continuation.exists() else stalled_review_feedback(report, review_dir) or inconclusive_feedback(report, review_dir)
                    if report.run_status != "completed" and stalled_feedback is None:
                        state.status = "incomplete"
                        interruption = review_dir / "interruption.json"
                        if (
                            interruption.exists()
                            and read_json(interruption).get("reason") == "quota_exhausted"
                        ):
                            atomic_json(
                                directory / "interruption.json",
                                {
                                    **read_json(interruption),
                                    "phase": phase,
                                    "round": index + 1,
                                    "branch": branch_id,
                                    "review_artifact": str(interruption.relative_to(directory)),
                                },
                            )
                            state.error = RESUME_MESSAGE
                            self.emit(
                                directory,
                                "quota_exhausted",
                                phase=phase,
                                round=index + 1,
                                branch=branch_id,
                                resume="manual",
                            )
                        else:
                            state.error = "Verification or its audit is unfinished. Resume this run before revising the proof."
                        return save()
                    if stalled_feedback is not None and "stalled_jobs" in stalled_feedback:
                        self.emit(directory, "review_stalled", round=index + 1, branch=branch_id, mode=mode, stalled_jobs=stalled_feedback["stalled_jobs"], decision=report.decision)
                    elif stalled_feedback is not None:
                        self.emit(directory, "review_inconclusive", round=index + 1, branch=branch_id, mode=mode, decision=report.decision)
                    branch["feedback"] = stalled_feedback or generator_feedback(report)
                    if continuation.exists() and read_json(turn_dir / "feedback.json") != branch["feedback"]:
                        raise FrozenRunMismatch("stalled continuation evidence changed")
                    atomic_json(turn_dir / "feedback.json", branch["feedback"])
                    if stalled_feedback is not None and not continuation.exists():
                        atomic_json(turn_dir / "stalled-report.json", report.model_dump(mode="json"))
                        atomic_json(continuation, {"report_sha256": sha(turn_dir / "stalled-report.json"), "feedback_sha256": sha(turn_dir / "feedback.json")})
                    if mode == "full_solution" and report.decision == "READY_FOR_HUMAN":
                        handoff_path = directory / "blueprint_ready_for_human.md"
                        atomic_text(handoff_path, candidate)
                        receipt = {
                            "decision": report.decision,
                            "human_confirmed": False,
                            "manifest_sha256": sha(directory / "manifest.json"),
                            "question_sha256": hashlib.sha256(question.encode()).hexdigest(),
                            "proof_sha256": sha(handoff_path),
                            "proof_artifact": handoff_path.name,
                            "review_artifact": str(
                                (review_dir / "report.json").relative_to(directory)
                            ),
                            "review_sha256": sha(review_dir / "report.json"),
                            "generation_artifact": str(
                                (turn_dir / "generation.json").relative_to(directory)
                            ),
                            "generation_sha256": sha(turn_dir / "generation.json"),
                            "rounds_completed": index + 1,
                            "generation_branches": len(branches),
                            "generation_turns_completed": state.generation_turns_completed,
                            "candidates_generated": state.candidates_generated,
                            "winning_branch": branch_id,
                        }
                        atomic_json(directory / "handoff.json", receipt)
                        self.emit(
                            directory,
                            "ready_for_human",
                            round=index + 1,
                            branch=branch_id,
                            proof_sha256=receipt["proof_sha256"],
                        )
                        for skipped in range(branch_id + 1, len(branches) + 1):
                            self.emit(
                                directory,
                                "verification_skipped",
                                round=index + 1,
                                branch=skipped,
                                reason="human_handoff_available",
                            )
                        return self.check_handoff(directory)
                    # A replayed review must not overwrite a later interrupted draft.
                    next_turn = branch["directory"] / "rounds" / f"{index + 2:04d}"
                    if not next_turn.exists():
                        workspace_files.write_text(
                            branch["directory"] / "generator", "results/blueprint.md", candidate
                        )
                state.rounds_completed = index + 1
                save()
            state.status = "budget_exhausted"
            self.emit(directory, "budget_exhausted", rounds_completed=state.rounds_completed)
            return save()
        except QuotaExhausted as exc:
            record_quota(directory, exc, phase=phase)
            state.status, state.error = "incomplete", str(exc)
            self.emit(directory, "quota_exhausted", phase=phase, resume="manual")
            return save()
        except KeyboardInterrupt:
            state.status, state.error = "incomplete", "Interrupted; resume with the same inputs."
            save()
            raise
        except Exception as exc:
            state.status, state.error = "incomplete", f"{type(exc).__name__}: {error_text(exc)}"
            self.emit(directory, "execution_error", error=state.error)
            return save()

    def branch_directory(self, directory, branch):
        # Keep the original layout for width one.
        return (
            directory
            if self.config.generation_branches == 1
            else directory / "branches" / f"{branch:04d}"
        )

    def verify_candidate(self, request, review_dir, references=None):
        verifier = self.verifier
        if isinstance(verifier.backend, CodexBackend):
            # Rejection cancels a review's backend. Its stop state must not leak to
            # another candidate/branch; a fresh instance still reuses disk caches.
            backend = replace(verifier.backend)
            if backend.identity != verifier.backend.identity:
                raise FrozenRunMismatch("verifier backend changed during experiment")
            verifier = type(verifier)(backend, verifier.config)
        if references is None:
            return verifier.verify(request, review_dir)
        return verifier.verify(request, review_dir, references=references)

    @staticmethod
    def seed_record(directory, question, seed_from, seed_branch):
        """Provenance of the draft a new run starts from, taken from an earlier run.

        Read from the parent only when the run is created; afterwards the frozen manifest
        is authoritative, and a seed can be neither added to nor changed in a run.
        """
        if seed_branch is not None and seed_from is None:
            raise ValueError("--seed-branch requires --seed-from")
        if (directory / "manifest.json").exists():
            seed = read_json(directory / "manifest.json")["provenance"].get("seed")
            if seed_from is not None and (
                seed is None
                or Path(seed["run_dir"]) != seed_from.expanduser().resolve()
                or seed_branch not in (None, seed["branch"])
            ):
                raise FrozenRunMismatch("a run's seed cannot be added or changed; use a new run directory")
            return seed
        if seed_from is None:
            return None
        parent = seed_from.expanduser().resolve()
        if parent == directory or not (parent / "manifest.json").is_file():
            raise ValueError("--seed-from must name another existing Rethlas-OSV run directory")
        provenance = read_json(parent / "manifest.json")["provenance"]
        if "generator" not in provenance:
            raise ValueError("--seed-from must name another existing Rethlas-OSV run directory")
        if provenance["question"] != question:
            raise ValueError("the seed run has a different problem")
        width = provenance.get("generation_branches", 1)
        branch = seed_branch if seed_branch is not None else 1 if width == 1 else None
        if branch is None or not 1 <= branch <= width:
            raise ValueError(f"the seed run has {width} branches; choose one with --seed-branch")
        branch_dir = parent if width == 1 else parent / "branches" / f"{branch:04d}"
        drafts = sorted(branch_dir.glob("rounds/*/generation.json"))
        if not drafts:
            raise ValueError("the seed run has no generated draft yet")
        saved = read_json(drafts[-1])
        if digest(saved["output"]) != saved["sha256"]:
            raise FrozenRunMismatch("the seed run's generation artifact changed")
        return {
            "run_dir": str(parent),
            "manifest_sha256": sha(parent / "manifest.json"),
            "branch": branch,
            "generation_artifact": drafts[-1].relative_to(parent).as_posix(),
            "generation_sha256": sha(drafts[-1]),
            "candidate_sha256": hashlib.sha256(
                saved["output"]["candidate_solution"].encode()
            ).hexdigest(),
        }

    def plant_seed(self, directory, branches, seed):
        """Round 1 of every branch is the parent's draft, reviewed without a generation call.

        Generation then starts a fresh session in round 2: the parent's session belongs to
        another workspace.
        """
        parent = read_json(directory / "inputs/seed/generation.json")["output"]
        output = {
            "turn": parent["turn"],
            "session_id": None,
            "candidate_solution": parent["candidate_solution"],
        }
        planted = {"output": output, "sha256": digest(output), "seed": "inputs/seed/generation.json"}
        for branch_id, branch in branches.items():
            path = branch["directory"] / "rounds/0001/generation.json"
            if path.exists():
                if read_json(path) != planted:
                    raise FrozenRunMismatch("seeded draft changed")
                continue
            atomic_json(path, planted)
            self.emit(
                directory,
                "seeded_from_run",
                branch=branch_id,
                parent_run=seed["run_dir"],
                parent_branch=seed["branch"],
                parent_artifact=seed["generation_artifact"],
                candidate_sha256=seed["candidate_sha256"],
            )

    def generate_wave(self, directory, branches, index, search, question):
        # Validate/replay caches before dispatching any new model calls in this wave.
        outputs = {}
        for branch_id, branch in branches.items():
            path = branch["directory"] / "rounds" / f"{index + 1:04d}" / "generation.json"
            if path.exists():
                outputs[branch_id] = self.generate_turn(
                    directory, branch_id, branch, index, search, question
                )
        if len(outputs) == len(branches):
            return outputs
        if len(branches) == 1:
            try:
                return {1: self.generate_turn(directory, 1, branches[1], index, search, question)}
            except BaseException:
                self.cancel_generation()
                raise
        pool = ThreadPoolExecutor(max_workers=len(branches))
        futures, errors = {}, []
        try:
            for branch_id, branch in branches.items():
                if branch_id in outputs:
                    continue
                future = pool.submit(
                    self.generate_turn, directory, branch_id, branch, index, search, question
                )
                futures[future] = branch_id
            for future in as_completed(futures):
                try:
                    outputs[futures[future]] = future.result()
                except Exception as exc:
                    errors.append(exc)
                    if isinstance(exc, QuotaExhausted):
                        halt = getattr(self.generator, "halt_for_quota", None)
                        if halt:
                            halt(exc)
            # Drain workers so completed candidates are checkpointed before returning.
            if errors:
                raise next((exc for exc in errors if isinstance(exc, QuotaExhausted)), errors[0])
            return outputs
        except BaseException:
            # A repeated Ctrl+C is held until every owned process group is stopped.
            with deferred_sigint():
                self.cancel_generation()
                for future in futures:
                    future.cancel()
            raise
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

    def cancel_generation(self):
        cancel = getattr(self.generator, "cancel", None)
        if cancel:
            cancel()

    def generate_turn(self, directory, branch_id, branch, index, search, question):
        workspace = branch["directory"] / "generator"
        turn_dir = branch["directory"] / "rounds" / f"{index + 1:04d}"
        turn_dir.mkdir(parents=True, exist_ok=True)
        session_id, feedback = branch["session_id"], branch["feedback"]
        if (turn_dir / "generation.json").exists():
            saved = read_json(turn_dir / "generation.json")
            if digest(saved["output"]) != saved["sha256"]:
                raise FrozenRunMismatch("cached generation artifact changed")
            generated = saved["output"]
            turn = GenerationTurn.model_validate(generated["turn"])
            session_id = generated["session_id"]
        else:
            workspace_files.write_text(workspace, "problem.md", question)
            if feedback is not None:
                atomic_json(workspace_files.destination(workspace, "feedback.json"), feedback)
            else:
                (workspace / "feedback.json").unlink(missing_ok=True)
            attempts = sorted(turn_dir.glob("attempt-*"))
            for attempt in reversed(attempts):
                meta = attempt / "events-summary.json"
                if meta.exists() and len(read_json(meta)["thread_ids"]) == 1:
                    session_id = read_json(meta)["thread_ids"][0]
                    break
            # No attempt of this turn has succeeded, so the latest rejection still applies.
            rejections = [a / "output-rejected.json" for a in attempts]
            rejected = next((read_json(r)["reason"] for r in reversed(rejections) if r.exists()), None)
            attempt = turn_dir / f"attempt-{len(attempts) + 1:04d}"
            first_round = branch["directory"] / "rounds/0001/generation.json"
            prompt = self.prompt(
                index, search, feedback, interrupted=bool(attempts), rejected=rejected,
                seeded=index == 1 and "seed" in read_json(first_round),
            )
            if self.config.generation_branches > 1:
                prompt = (
                    f"You are independent search branch {branch_id} of {self.config.generation_branches}. "
                    "Develop your own complete solution trajectory. Choose and record a promising "
                    "approach using your own reasoning; explore alternatives when it stalls. "
                    "Use only this workspace's memory, draft, and feedback. Do not inspect sibling "
                    "branches or share their sessions or results.\n" + prompt
                )
            self.emit(
                directory,
                "generation_started",
                round=index + 1,
                branch=branch_id,
                search=search,
                resume_session=session_id,
            )
            try:
                turn, session_id = self.generator.generate(
                    workspace, attempt, prompt, session_id, search
                )
                candidate = self.read_candidate(workspace)
            except BaseException as exc:
                if isinstance(exc, QuotaExhausted):
                    record_quota(attempt, exc, branch=branch_id, round=index + 1)
                if isinstance(exc, InvalidGenerationOutput):
                    atomic_json(attempt / "output-rejected.json", {"reason": exc.reason})
                reason = (
                    "quota_exhausted"
                    if isinstance(exc, QuotaExhausted)
                    else "cancelled"
                    if isinstance(exc, (BackendCancelled, KeyboardInterrupt))
                    else "invalid_output"
                    if isinstance(exc, InvalidGenerationOutput)
                    else "execution_error"
                )
                self.emit(
                    directory,
                    "generation_interrupted",
                    branch=branch_id,
                    round=index + 1,
                    reason=reason,
                    error=error_text(exc),
                    attempt_artifact=str(attempt.relative_to(directory)),
                )
                raise
            generated = {
                "turn": turn.model_dump(mode="json"),
                "session_id": session_id,
                "candidate_solution": candidate,
            }
            atomic_json(
                turn_dir / "generation.json", {"output": generated, "sha256": digest(generated)}
            )
        self.emit(
            directory,
            "generation_finished",
            round=index + 1,
            branch=branch_id,
            candidate_ready=turn.candidate_ready,
            session_id=session_id,
        )
        return generated

    @staticmethod
    def read_candidate(workspace):
        path = workspace / "results/blueprint.md"
        if not path.resolve().is_relative_to(workspace) or not path.is_file():
            raise InvalidGenerationOutput(
                "results/blueprint.md was missing or resolved outside the workspace"
            )
        try:
            candidate = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise InvalidGenerationOutput("results/blueprint.md could not be decoded as text") from None
        if not candidate.strip():
            raise InvalidGenerationOutput("results/blueprint.md was empty")
        return candidate

    def prepare_workspace(self, workspace, directory, question, frozen):
        workspace.mkdir(parents=True, exist_ok=True)
        # Restore maintained instructions on every invocation; preserve the experiment's
        # memory/results. These unsandboxed writes replace, never follow, workspace links.
        workspace_files.copy_tree(
            ROOT / "vendor/generation/.agents/skills", workspace, ".agents/skills"
        )
        workspace_files.copy_tree(ROOT / "verify-proof", workspace, ".agents/skills/verify-proof")
        workspace_files.copy_file(ROOT / "generation-AGENTS.md", workspace, "AGENTS.md")
        workspace_files.write_text(workspace, "problem.md", question)
        workspace_files.real_directory(workspace, "results")
        if frozen["references"]:
            workspace_files.copy_tree(directory / "inputs/references", workspace, "references")
            for rel in frozen["references"]:
                if Path(rel).suffix.lower() == ".pdf":
                    binary = shutil.which("pdftotext")
                    if binary is None:
                        raise RuntimeError(
                            "PDF references require pdftotext; install Poppler or supply extracted text"
                        )
                    workspace_files.extract_pdf(
                        binary, workspace, f"references/{rel}", f"references/.extracted/{rel}.txt"
                    )
        identity = self.generator.identity
        config = (
            f"model = {json.dumps(identity.get('model', DEFAULT_MODEL))}\n"
            f"model_reasoning_effort = {json.dumps(identity.get('reasoning_effort', 'xhigh'))}\n"
            'developer_instructions = "Work on your assigned Rethlas plan, persist branch-local memory and report to the parent. Do not submit the root blueprint or create final handoff artifacts."\n'
        )
        workspace_files.write_text(workspace, ".codex/agents/subgoal-prover.toml", config)

    @staticmethod
    def prompt(index, search, feedback, *, interrupted=False, rejected=None, seeded=False):
        start = (
            "Resume the interrupted generation turn."
            if interrupted
            else "Continue from the draft in results/blueprint.md, which this run took from an earlier run of the same problem; this workspace's memory starts empty."
            if seeded
            else "Continue the same mathematical investigation."
            if index
            else "Develop a solution to the complete question in problem.md."
        )
        if rejected:
            start += (
                f" The previous attempt ended without a valid submission: {rejected}."
                " Correct this before ending the turn."
            )
        instructions = (
            start
            + " Follow AGENTS.md and the Rethlas skills. Use problem_id=problem and references/ if present. "
            f"This is generation round {index + 1}; external search is {search}. "
            "Persist progress in local memory. Always save the entire current draft, including partial progress and explicit remaining obligations, to results/blueprint.md. "
            "Submit only a full candidate by returning candidate_ready=true in the required JSON. "
            "For partial progress, return false. Every turn is reviewed: false selects partial-progress review, true selects full-solution review. Partial PASS permits continuing, not completion or certification. Partial REVISE requests corrections without guaranteeing repairability; partial REJECT concerns an audited obstruction to the current route. Do not wait until you feel ready for verification: end at a useful progress checkpoint. The Python orchestrator runs OSVerify after this turn; do not run a verifier or create a verified file yourself."
        )
        if feedback is not None:
            instructions += (
                "\nThe previous review (possibly incomplete; inspect run_status and continuation_reason) is saved in feedback.json. Here is its exact released content:\n"
                + json.dumps(feedback, ensure_ascii=False)
            )
        return instructions

    @staticmethod
    def check_handoff(directory):
        receipt = read_json(directory / "handoff.json")
        if receipt["decision"] != "READY_FOR_HUMAN" or receipt["human_confirmed"] is not False:
            raise FrozenRunMismatch("invalid handoff semantics")
        for path_key, hash_key in (
            ("proof_artifact", "proof_sha256"),
            ("review_artifact", "review_sha256"),
            ("generation_artifact", "generation_sha256"),
        ):
            path = (directory / receipt[path_key]).resolve()
            if not path.is_relative_to(directory) or sha(path) != receipt[hash_key]:
                raise FrozenRunMismatch("handoff artifact changed")
        if sha(directory / "manifest.json") != receipt["manifest_sha256"]:
            raise FrozenRunMismatch("handoff provenance changed")
        report = read_report(read_json(directory / receipt["review_artifact"]))
        generated = read_json(directory / receipt["generation_artifact"])["output"]
        if (
            report.decision != "READY_FOR_HUMAN"
            or report.run_status != "completed"
            or report.feedback
        ):
            raise FrozenRunMismatch("handoff no longer has an eligible review")
        candidate = generated["candidate_solution"]
        question = read_json(directory / "manifest.json")["provenance"]["question"]
        if (
            hashlib.sha256(candidate.encode()).hexdigest() != receipt["proof_sha256"]
            or hashlib.sha256(question.encode()).hexdigest() != receipt["question_sha256"]
        ):
            raise FrozenRunMismatch("handoff does not match frozen question and proof")
        request = read_json((directory / receipt["review_artifact"]).parent / "request.json")
        if request["question"] != question or request["candidate_solution"] != candidate:
            raise FrozenRunMismatch("handoff review refers to different input")
        state = ExperimentResult(
            status="ready_for_human",
            rounds_completed=receipt["rounds_completed"],
            generation_branches=receipt.get("generation_branches", 1),
            generation_turns_completed=receipt.get("generation_turns_completed", 0),
            candidates_generated=receipt.get("candidates_generated", 0),
            winning_branch=receipt.get("winning_branch"),
            last_decision="READY_FOR_HUMAN",
            handoff_artifact=receipt["proof_artifact"],
        )
        atomic_json(directory / "summary.json", state.model_dump(mode="json"))
        return state
