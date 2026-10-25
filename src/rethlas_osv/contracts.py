from __future__ import annotations

from typing import Literal

from osverifier.jobs import TIMEOUT_FAILURES
from osverifier.models import ReviewReport, StrictModel
from osverifier.workflow_models import WorkflowReport
from pydantic import Field


class InvalidGenerationOutput(ValueError):
    """A finished turn without a usable submission. The reason is orchestrator text only,
    so it can be repeated to the generator when the interrupted turn resumes."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class GenerationTurn(StrictModel):
    candidate_ready: bool
    progress: str = Field(min_length=1)


class ExperimentResult(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    status: Literal["running", "ready_for_human", "budget_exhausted", "incomplete"]
    rounds_completed: int
    generation_branches: int = 1
    generation_turns_completed: int = 0
    candidates_generated: int = 0
    winning_branch: int | None = None
    last_decision: Literal["READY_FOR_HUMAN", "PASS", "REVISE", "REJECT"] | None = None
    human_confirmed: Literal[False] = False
    handoff_artifact: str | None = None
    error: str | None = None


def read_report(value: dict) -> ReviewReport:
    cls = WorkflowReport if value.get("schema_version") == "3.0" else ReviewReport
    return cls.model_validate(value)


def generator_feedback(report: ReviewReport) -> dict:
    """A deterministic adapter; no rationale or unaudited text is passed through."""
    if report.run_status != "completed":
        raise ValueError("unfinished verification must be resumed before generation continues")
    return {
        "decision": report.decision,
        "mode": report.mode,
        "run_status": report.run_status,
        "feedback": [item.model_dump(mode="json") for item in report.feedback],
        "human_confirmed": False,
    }


def stalled_review_feedback(report: ReviewReport, directory):
    """Allow continuation only for exhausted timeout jobs, never audit/contract failure.

    Either mode continues; an unfinished review never hands off, and resubmitting the
    same draft resumes the review, retrying only its unfinished jobs.
    """
    if report.run_status == "completed":
        return None
    if (directory / "interruption.json").exists():
        return None
    if not (directory / "jobs").is_dir():
        return None
    failed = []
    for job in (directory / "jobs").iterdir():
        if not job.is_dir() or (job / "result.json").exists():
            continue
        attempts = sorted(job.glob("attempt-*"))
        if not attempts:
            continue
        last = attempts[-1]
        failure = last / "failure.json"
        if not failure.exists():
            return None
        from osverifier.storage import read_json

        if read_json(failure).get("type") not in TIMEOUT_FAILURES:
            return None
        failed.append(job.name)
    if not failed:
        return None
    return {
        "mode": report.mode,
        "decision": report.decision,
        "run_status": report.run_status,
        "continuation_reason": "verification_stalled",
        "review_complete": False,
        "notice": (
            "This review did not finish after bounded timeout retries. Continue with the exact audited feedback below; no PASS or correctness claim is implied. An existing REJECT still requires reconsidering the current route."
            if report.mode == "partial"
            else "This full-solution review did not finish after bounded timeout retries, so this round cannot hand off. Continue with the exact audited feedback below; no READY_FOR_HUMAN or correctness claim is implied. If you judge the submitted draft complete and correct, you may submit it unchanged: its review then resumes, retrying only the unfinished tasks."
        ),
        "feedback": [item.model_dump(mode="json") for item in report.feedback],
        "human_confirmed": False,
        "stalled_jobs": failed,
    }


def inconclusive_feedback(report: ReviewReport, directory):
    """Continue after a finished review that cannot conclude, e.g. on an unlocatable source.

    Its jobs all have exact cached results, so resuming replays the same report; blocking
    on it would stop the experiment for good. Never a handoff: the report stays incomplete.
    """
    if report.run_status == "completed" or (directory / "interruption.json").exists():
        return None
    if getattr(report, "termination_reason", None) != "review_inconclusive":
        return None
    return {
        "mode": report.mode,
        "decision": report.decision,
        "run_status": report.run_status,
        "continuation_reason": "verification_inconclusive",
        "review_complete": False,
        "notice": "This review finished every planned task but could not reach a conclusion: some checks stayed unresolved, for example a cited result whose source could not be located or read. Resubmitting the same draft returns the same result. Continue with the exact audited feedback below; no PASS or correctness claim is implied. Where the argument depends on an external result, state it exactly and either prove it or cite a source that can be checked.",
        "feedback": [item.model_dump(mode="json") for item in report.feedback],
        "human_confirmed": False,
    }
