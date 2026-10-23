"""A finished but inconclusive review (e.g. an unlocatable cited source) must not park the loop."""

import json

import pytest
from osverifier.storage import read_json
from osverifier.workflow import DecomposingVerifier, WorkflowConfig
from test_rethlas import Generator, problem
from workflow_fixtures import FixtureBackend

from rethlas_osv.runner import Experiment, LoopConfig

PROOF = "".join(f"Step {i} applies the cited classical lemma.\n" for i in range(1, 7))


class UnlocatableSource(FixtureBackend):
    def __init__(self, cause):
        super().__init__({"root": ["UNAUDITED ALLEGATION"]} if cause != "search" else {})
        self.cause = cause

    def _generate(self, role, payload, schema, attempt_dir):
        result = super()._generate(role, payload, schema, attempt_dir)
        partial = payload["request"]["mode"] == "partial"
        if role == "proposer" and self.cause == "search" and payload["scope"]["scope_id"] == "root":
            result.update(search_status="incomplete", search_limitations=["UNAUDITED LIMITATION"])
        if role == "issue_validator":
            if self.cause == "failure":
                raise ValueError("fixture contract failure")
            result.update(assessment="unresolved", impact="unknown", completion_effect="uncertain",
                          rejection_supported=False, feedback_candidates=[])
            if partial:
                result["route_obstruction"] = ""
        if role == "feedback_auditor" and partial:
            result.update(route_rejection_upheld=False, route_rationale="Fixture route audit")
        return result


def experiment(backend, generator, rounds):
    verifier = DecomposingVerifier(backend, WorkflowConfig(workers=1, max_attempts=1))
    return Experiment(generator, verifier, LoopConfig(rounds))


@pytest.mark.parametrize("ready", [True, False])
@pytest.mark.parametrize("cause", ["unresolved", "search"])
def test_inconclusive_review_continues_generation_without_handoff(tmp_path, cause, ready):
    p, run, backend = problem(tmp_path), tmp_path / "run", UnlocatableSource(cause)
    g = Generator([(ready, PROOF)] * 3)
    result = experiment(backend, g, 2).run(p, run)
    assert result.status == "budget_exhausted" and result.rounds_completed == 2, result.error
    assert len(g.calls) == 2 and not (run / "handoff.json").exists()
    feedback = read_json(run / "rounds/0001/feedback.json")
    assert feedback["continuation_reason"] == "verification_inconclusive"
    assert feedback["mode"] == ("full_solution" if ready else "partial")
    assert feedback["run_status"] == "incomplete" and feedback["decision"] is None
    assert feedback["feedback"] == [] and feedback["human_confirmed"] is False
    assert "verification_inconclusive" in g.calls[1]["prompt"]
    # Only orchestrator text and exact audited feedback cross into generation.
    assert "UNAUDITED" not in g.calls[1]["prompt"]
    events = [json.loads(line)["event"] for line in (run / "trajectory.jsonl").read_text().splitlines()]
    assert events.count("review_inconclusive") == 2
    # The identical draft replays its cached review; only generation advances on resume.
    model_calls = len(backend.calls)
    result = experiment(backend, g, 3).run(p, run)
    assert result.status == "budget_exhausted" and result.rounds_completed == 3
    assert len(g.calls) == 3 and len(backend.calls) == model_calls


def test_unfinished_review_still_blocks_generation(tmp_path):
    g = Generator([(True, PROOF)] * 2)
    result = experiment(UnlocatableSource("failure"), g, 2).run(problem(tmp_path), tmp_path / "run")
    assert result.status == "incomplete" and len(g.calls) == 1
    assert result.error.startswith("Verification or its audit is unfinished")
