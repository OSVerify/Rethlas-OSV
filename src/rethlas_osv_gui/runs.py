"""Read-only inspection of run directories produced by the CLIs."""

from __future__ import annotations

import json
import re
from pathlib import Path

from osverifier.storage import read_json

from .jobs import JOB_FILE, STDERR_LOG, JobManager, read_job

INPUTS_DIR = "gui-inputs"
SLUG = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    slug = SLUG.sub("-", name.strip().lower()).strip("-")
    return slug[:80]


def unique_run_dir(runs_root: Path, name: str) -> Path:
    slug = slugify(name) or "run"
    candidate = runs_root / slug
    counter = 2
    while candidate.exists():
        candidate = runs_root / f"{slug}-{counter}"
        counter += 1
    return candidate


def safe_run_dir(runs_root: Path, name: str) -> Path:
    if not name or "/" in name or name in (".", "..") or name.startswith("."):
        raise ValueError("invalid run name")
    directory = (runs_root / name).resolve()
    if directory.parent != runs_root.resolve():
        raise ValueError("invalid run name")
    return directory


def _json(path: Path):
    try:
        return read_json(path)
    except (OSError, ValueError):
        return None


def _text(path: Path, limit: int | None = None) -> str | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if limit is not None and len(text) > limit:
        return text[-limit:]
    return text


def detect_kind(run_dir: Path) -> str | None:
    job = read_job(run_dir)
    if job and job.get("kind") in ("verify", "prove"):
        return job["kind"]
    if (run_dir / "summary.json").exists() or (run_dir / "inputs" / "problem.md").exists():
        return "prove"
    if (run_dir / "request.json").exists() or (run_dir / "report.json").exists():
        return "verify"
    if (run_dir / "manifest.json").exists():
        manifest = _json(run_dir / "manifest.json") or {}
        provenance = manifest.get("provenance", {})
        return "prove" if "generator" in provenance else "verify"
    return None


def _quota(run_dir: Path) -> dict | None:
    interruption = _json(run_dir / "interruption.json") or _json(run_dir / ".verification" / "interruption.json")
    if interruption and interruption.get("reason") == "quota_exhausted":
        return interruption
    return None


def _verify_status(run_dir: Path, job: dict | None) -> dict:
    report = _json(run_dir / "report.json")
    quota = _quota(run_dir)
    result = {"decision": None, "error": None, "resumable": False}
    if report is not None:
        result["decision"] = report.get("decision")
        result["review_kind"] = report.get("review_kind", "solution")
        if report.get("classification_status") in ("failed", "withheld", "pending"):
            result["resumable"] = True
            result["error"] = report.get("classification_error")
        result["coverage_complete"] = report.get("coverage_complete")
        result["termination_reason"] = report.get("termination_reason")
        if report.get("interruption_reason") == "quota_exhausted":
            result.update(status="quota_exhausted", resumable=True, error="Quota exhausted; partial report preserved. Resume after quota recovers.")
            return result
        if report.get("run_status") == "completed":
            result["status"] = "completed"
            return result
        result["resumable"] = True
        if quota:
            result["status"] = "quota_exhausted"
            result["error"] = quota.get("detail") or quota.get("message")
        else:
            result["status"] = "incomplete"
            result["error"] = "; ".join(report.get("unfinished", []) or []) or None
        return result
    if quota and not (run_dir / "manifest.json").exists():
        # Quota ran out while the question was being extracted from the paper.
        result["status"], result["resumable"] = "quota_exhausted", True
        result["error"] = quota.get("detail") or quota.get("message")
        return result
    if job and job.get("status") == "exited" and job.get("exit_code") not in (0, None):
        result["status"] = "failed"
        result["resumable"] = job.get("exit_code") == 130
        return result
    if (run_dir / "manifest.json").exists():
        result["status"], result["resumable"] = "interrupted", True
        return result
    if job and job.get("status") == "running":
        result["status"] = "running"
        return result
    result["status"] = "not_started"
    return result


def _prove_status(run_dir: Path, job: dict | None) -> dict:
    summary = _json(run_dir / "summary.json")
    handoff = _json(run_dir / "handoff.json")
    quota = _quota(run_dir)
    result = {"decision": None, "error": None, "resumable": False, "summary": summary}
    if handoff is not None:
        result["status"], result["decision"] = "ready_for_human", handoff.get("decision")
        return result
    if summary is None:
        if job and job.get("status") == "exited" and job.get("exit_code") not in (0, None):
            result["status"] = "failed"
            return result
        result["status"] = "not_started"
        return result
    result["decision"] = summary.get("last_decision")
    status = summary.get("status")
    if status == "budget_exhausted":
        result["status"], result["resumable"] = "budget_exhausted", True
    elif status == "incomplete":
        result["resumable"] = True
        if quota:
            result["status"] = "quota_exhausted"
            result["error"] = quota.get("detail") or quota.get("message")
        else:
            result["status"] = "incomplete"
            result["error"] = summary.get("error")
    elif status == "ready_for_human":
        result["status"] = "ready_for_human"
    else:
        result["status"], result["resumable"] = "interrupted", True
    return result


def run_summary(run_dir: Path, manager: JobManager) -> dict | None:
    kind = detect_kind(run_dir)
    if kind is None:
        return None
    job = read_job(run_dir)
    active = manager.is_active(run_dir)
    state = _prove_status(run_dir, job) if kind == "prove" else _verify_status(run_dir, job)
    if active:
        state["status"], state["resumable"] = "running", False
    try:
        updated = max(
            p.stat().st_mtime
            for p in (
                run_dir / "trajectory.jsonl",
                run_dir / "report.json",
                run_dir / "summary.json",
                run_dir / JOB_FILE,
                run_dir,
            )
            if p.exists()
        )
    except ValueError:
        updated = 0
    return {
        "name": run_dir.name,
        "path": str(run_dir),
        "kind": kind,
        "active": active,
        "updated_at": updated,
        "job": job,
        **state,
    }


def list_runs(runs_root: Path, manager: JobManager) -> list[dict]:
    if not runs_root.is_dir():
        return []
    rows = []
    for child in runs_root.iterdir():
        if child.is_dir() and not child.name.startswith("."):
            summary = run_summary(child, manager)
            if summary is not None:
                rows.append(summary)
    rows.sort(key=lambda row: row["updated_at"], reverse=True)
    return rows


def _reviews(run_dir: Path) -> list[dict]:
    rows = []
    for report_path in sorted((run_dir / "reviews").glob("*/report.json")):
        report = _json(report_path) or {}
        rows.append(
            {
                "key": report_path.parent.name,
                "decision": report.get("decision"),
                "run_status": report.get("run_status"),
                "feedback_count": len(report.get("feedback", []) or []),
                "updated_at": report_path.stat().st_mtime,
            }
        )
    rows.sort(key=lambda row: row["updated_at"])
    return rows


def _inputs(run_dir: Path, kind: str) -> dict:
    if kind == "prove":
        problem = _text(run_dir / "inputs" / "problem.md")
        if problem is None:
            problem = _text(run_dir / INPUTS_DIR / "problem.md")
        return {"problem": problem}
    question = _text(run_dir / "question.md")
    solution = _text(run_dir / "candidate-solution.md")
    if question is None:
        question = _text(run_dir / INPUTS_DIR / "question.md")
    if solution is None:
        solution = _text(run_dir / INPUTS_DIR / "solution.md")
    if solution is None:
        for paper in sorted((run_dir / INPUTS_DIR).glob("paper.*")):
            solution = _text(paper)
    request = _json(run_dir / "request.json") or {}
    return {"question": question or request.get("question"), "solution": solution or request.get("candidate_solution")}


def run_detail(run_dir: Path, manager: JobManager) -> dict | None:
    summary = run_summary(run_dir, manager)
    if summary is None:
        return None
    kind = summary["kind"]
    detail = {
        **summary,
        "inputs": _inputs(run_dir, kind),
        "stderr_tail": _text(run_dir / STDERR_LOG, 6000),
    }
    if kind == "prove":
        reviews = _reviews(run_dir)
        detail["reviews"] = reviews
        latest = reviews[-1]["key"] if reviews else None
        detail["report_markdown"] = (
            _text(run_dir / "reviews" / latest / "report.md") if latest else None
        )
        detail["latest_review"] = latest
        detail["proof_text"] = _text(run_dir / "blueprint_ready_for_human.md")
        detail["handoff"] = _json(run_dir / "handoff.json")
    else:
        detail["report_markdown"] = _text(run_dir / "report.md")
        detail["report"] = _json(run_dir / "report.json")
        detail["extraction"] = _json(run_dir / INPUTS_DIR / "extraction.json")
        detail["paper_mode"] = _paper_mode(run_dir)
    return detail


def _paper_mode(run_dir: Path) -> str | None:
    job = read_job(run_dir)
    argv = (job or {}).get("argv") or []
    if any(m in argv for m in ("osverifier_reports.paper_prepare", "osverifier_gui.paper")) and "--mode" in argv:
        return argv[argv.index("--mode") + 1]
    return None


def review_detail(run_dir: Path, key: str) -> dict | None:
    if "/" in key or key.startswith("."):
        raise ValueError("invalid review key")
    review_dir = run_dir / "reviews" / key
    if not review_dir.is_dir():
        return None
    return {
        "key": key,
        "report": _json(review_dir / "report.json"),
        "report_markdown": _text(review_dir / "report.md"),
        "candidate_solution": _text(review_dir / "candidate-solution.md"),
    }


def trajectory_events(run_dir: Path, offset: int, limit: int = 400) -> dict:
    """Return trajectory lines after a byte offset; the client keeps the offset."""
    path = run_dir / "trajectory.jsonl"
    if (run_dir / "paper-task.json").exists():
        path = run_dir / ".verification" / "trajectory.jsonl"
    if not path.exists():
        return {"offset": 0, "events": [], "truncated": False}
    size = path.stat().st_size
    if offset > size:
        offset = 0  # the file was replaced; start over
    events = []
    with path.open("rb") as stream:
        stream.seek(offset)
        while True:
            line = stream.readline()
            if not line:
                break
            if not line.endswith(b"\n"):
                break  # partial write in progress; re-read it next time
            offset = stream.tell()
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
    truncated = len(events) > limit
    return {"offset": offset, "events": events[-limit:], "truncated": truncated}
