"""Helpers shared with OSVerify's tests/test_workflow.py (test functions removed)."""

import threading
from copy import deepcopy

from osverifier.backend import BackendCancelled
from osverifier.demo import feedback_decision
from osverifier.models import ReviewRequest
from osverifier.workflow import DecomposingVerifier, WorkflowConfig


class FixtureBackend:
    identity = {"kind": "scripted_demo"}

    def __init__(self, proposals=None, reject=(), veto=(), dedup=False, confirm_duplicate=True):
        self.proposals = proposals or {}
        self.reject, self.veto = set(reject), set(veto)
        self.dedup, self.confirm_duplicate = dedup, confirm_duplicate
        self.calls = []
        self.cancelled = threading.Event()
        self.slow_started = threading.Event()
        self.slow = False
        self.bad_partition = False

    def cancel(self):
        self.cancelled.set()

    def generate(self, role, payload, schema, attempt_dir):
        result = self._generate(role, payload, schema, attempt_dir)
        if payload.get("paper_review") and role == "proposer":
            result["evidence_limits"] = []
        if payload.get("paper_images"):
            result["visual_inspections"] = [
                {"image_id": i["image_id"], "status": "inspected", "observation": "Fixture pixels checked"}
                for i in payload["paper_images"]]
        return result

    def _generate(self, role, payload, schema, attempt_dir):
        if self.cancelled.is_set():
            raise BackendCancelled("fixture cancelled")
        self.calls.append((role, deepcopy(payload)))
        if role == "proposer":
            sid = payload["scope"]["scope_id"]
            return {
                "scope_id": sid,
                "search_status": "completed",
                "search_limitations": [],
                "issues": [
                    {
                        "issue_id": f"p{index}",
                        "scope_id": sid,
                        "kind": "invalid_inference",
                        "target": target,
                        "source_spans": [
                            {"document": "candidate_solution", "start_line": 1, "end_line": 1}
                        ],
                        "allegation": target,
                        "evidence": "Fixture evidence",
                        "proposed_feedback": [],
                    }
                    for index, target in enumerate(self.proposals.get(sid, []))
                ],
            }
        if role == "decomposer":
            return {
                "segments": [
                    {
                        "start_line": 1,
                        "end_line": 3,
                        "context_before": "",
                        "context_after": "Later source text.",
                    },
                    {
                        "start_line": 5 if self.bad_partition else 4,
                        "end_line": 6,
                        "context_before": "Earlier source text.",
                        "context_after": "",
                    },
                ]
            }
        if role == "deduplicator":
            earlier = payload["earlier_candidates"]
            incoming = payload["incoming_candidates"]
            return {
                "matches": [
                    {
                        "issue_id": incoming[0]["issue_id"],
                        "duplicate_of": earlier[0]["issue_id"],
                        "rationale": "Fixture duplicate link",
                    }
                ]
                if self.dedup and earlier
                else []
            }
        if role == "duplicate_auditor":
            return {
                "checks": [
                    {**m, "confirmed": self.confirm_duplicate}
                    for m in payload["proposed_matches"]["matches"]
                ]
            }
        if role == "issue_validator":
            issue = payload["proposed_issue"]
            if self.slow and issue["target"] == "slow":
                self.slow_started.set()
                assert self.cancelled.wait(5), "expected early cancellation"
                raise BackendCancelled("slow issue cancelled")
            if self.slow and issue["target"] == "reject":
                assert self.slow_started.wait(5)
            rejecting = issue["target"] in self.reject
            return {
                "issue_id": issue["issue_id"],
                "assessment": "confirmed",
                "kind": "invalid_inference" if rejecting else "omission",
                "impact": "blocking",
                "rejection_supported": rejecting,
                "completion_effect": "none" if rejecting else "missing_required_content",
                "source_spans": issue["source_spans"],
                "rationale": "Fixture rationale",
                "evidence": "Fixture evidence",
                "feedback_candidates": [
                    {
                        "feedback_id": "diagnosis",
                        "kind": "diagnosis",
                        "text": f"Audited fixture: {issue['target']}",
                    }
                ],
            }
        if role == "feedback_auditor":
            issue, review = payload["proposed_issue"], payload["issue_review"]
            approved = issue["target"] not in self.veto
            return {
                "issue_id": issue["issue_id"],
                "issue_review_assessment": "upheld" if approved else "disputed",
                "rationale": "Fixture audit",
                "decisions": [
                    feedback_decision(f["feedback_id"], approved)
                    for f in review["feedback_candidates"]
                ],
            }
        raise AssertionError(role)


def request():
    return ReviewRequest(
        request_id="fixture",
        question="Inspect this fixture.",
        candidate_solution="first\r\nsecond\r\nthird\r\nfourth\r\nfifth\r\nsixth\r\n",
    )


def run(backend, directory, workers=1):
    return DecomposingVerifier(backend, WorkflowConfig(workers=workers, max_attempts=1)).verify(
        request(), directory
    )






























CORE_TEXT_IO = r"""
import sys, warnings
from pathlib import Path
warnings.filterwarnings("error", category=EncodingWarning, module=r"osverifier(\.|$)")
sys.path.insert(0, sys.argv[1])
from workflow_fixtures import FixtureBackend, request
from osverifier.cli import main
from osverifier.connection_config import ConnectionConfig
from osverifier.quota import QuotaMonitor
from osverifier.workflow import DecomposingVerifier
root = Path(sys.argv[2])
math = request().model_copy(update={"question": "Show x ≤ y for all é."})
report = DecomposingVerifier(FixtureBackend({"root": ["gap ∑"]})).verify(math, root / "run")
assert report.decision == "REVISE", report
assert "∑" in (root / "run/report.md").read_bytes().decode("utf-8")
assert main(["export", "--run-dir", str(root / "run"), "--output", str(root / "r.tex")]) == 0
assert main(["demo", "--run-dir", str(root / "demo")]) in (0, 2)
(root / "q.md").write_bytes("x ≤ y".encode())
assert main(["verify", "--question", str(root / "q.md"), "--solution", str(root / "q.md"),
             "--model", "m", "--codex-bin", str(root / "absent"), "--run-dir", str(root / "v")]) == 1
(root / "home").mkdir()
(root / "home/config.toml").write_bytes('model_provider = "prøxy"\n'.encode())
assert ConnectionConfig(root / "home").values["model_provider"] == "prøxy"
(root / "attempt").mkdir()
(root / "attempt/stderr.log").write_bytes("ERROR: usage limit reached ≤".encode())
QuotaMonitor(root / "attempt").failed_stderr()
"""






