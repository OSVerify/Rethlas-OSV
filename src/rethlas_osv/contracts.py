from __future__ import annotations

from typing import Literal

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


def stalled_partial_feedback(report: ReviewReport, directory):
    """Allow continuation only for exhausted timeout jobs, never audit/contract failure."""
    if report.mode != "partial" or report.run_status == "completed":
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

        if read_json(failure).get("type") not in ("BackendStalled", "TimeoutExpired"):
            return None
        failed.append(job.name)
    if not failed:
        return None
    return {
        "mode": "partial",
        "decision": report.decision,
        "run_status": report.run_status,
        "continuation_reason": "verification_stalled",
        "review_complete": False,
        "notice": "This review did not finish after bounded timeout retries. Continue with the exact audited feedback below; no PASS or correctness claim is implied. An existing REJECT still requires reconsidering the current route.",
        "feedback": [item.model_dump(mode="json") for item in report.feedback],
        "human_confirmed": False,
        "stalled_jobs": failed,
    }
