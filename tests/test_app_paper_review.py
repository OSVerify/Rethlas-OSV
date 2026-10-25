from types import SimpleNamespace

from osverifier_reports import paper
from osverifier_reports.writer import Fidelity
from workflow_fixtures import FixtureBackend, request


class Classifier:
    """Scripted classifier: minor iff a released repair suggestion exists, unless ``by_kind`` is off."""

    def __init__(self, approve=True, fail=False, by_kind=True):
        self.approve, self.fail, self.by_kind, self.calls = approve, fail, by_kind, []

    def call(self, role, payload, schema, directory):
        self.calls.append(role)
        assert "decision" not in payload
        assert payload["paper"] == request().candidate_solution
        if self.fail:
            raise RuntimeError("classification failed")
        if schema is Fidelity:
            return Fidelity(approved=self.approve, reasons=[])
        ids = list(dict.fromkeys(f["issue_id"] for f in payload["feedback"]))
        suggested = {f["issue_id"] for f in payload["feedback"] if f["kind"] == "repair_suggestion"}
        # With by_kind, the internally rejected first finding is minor because its fix was released.
        minor = (lambda n, i: i in suggested) if self.by_kind else (lambda n, i: n == 0)
        return paper.Ratings(issues=[paper.Rating(issue_id=i, severity="minor" if minor(n, i) else "major", rationale="Fixture impact basis") for n, i in enumerate(ids)])


class RepairingBackend(FixtureBackend):
    """Fixture verifier that also releases a repair suggestion for the first proposed issue."""

    def generate(self, role, payload, schema, attempt_dir):
        result = super().generate(role, payload, schema, attempt_dir)
        if role == "issue_validator" and payload["proposed_issue"]["target"] == "reject":
            result["feedback_candidates"].append(
                {"feedback_id": "fix", "kind": "repair_suggestion", "text": "Fixture repair"}
            )
        return result


def args(tmp_path, early_stop=False):
    return SimpleNamespace(run_dir=tmp_path, decompose=True, early_stop=early_stop,
                           workers=1, max_issues=None, max_attempts=1, timeout_retries=0, model="test",
                           codex_bin="codex", codex_home=None)


def review(tmp_path, classifier=None, early_stop=False, backend_cls=None):
    backend = (backend_cls or RepairingBackend)({"root": ["reject"], "node-0001": ["local"]}, reject=["reject"])
    result = paper.run_paper(args(tmp_path, early_stop), request(), backend, classifier or Classifier())
    return result, backend


















def test_gui_shows_paper_summary_and_internal_activity(served):
    app, client = served
    run = app.runs_root / "paper"
    report, _ = review(run)
    code, detail = client.call("GET", "/api/runs/paper")
    assert code == 200 and detail["review_kind"] == "paper" and detail["decision"] is None
    assert detail["report"]["issues"] == report.model_dump()["issues"]
    assert detail["inputs"]["solution"] == request().candidate_solution
    code, events = client.call("GET", "/api/runs/paper/events")
    assert code == 200 and events["events"]






