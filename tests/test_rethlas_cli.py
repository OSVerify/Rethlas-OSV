"""Exercise CLI rejection-stop control through the real orchestration and verifier."""

import json

import pytest
from osverifier.storage import read_json
from test_rethlas import Generator, problem
from workflow_fixtures import FixtureBackend, request

from rethlas_osv import cli


@pytest.mark.parametrize("no_early_stop", [False, True])
@pytest.mark.parametrize("rejection_scope", ["root", "node-0001"])
def test_cli_controls_rejection_stop_and_freezes_setting(
    tmp_path, monkeypatch, no_early_stop, rejection_scope
):
    scopes = ["root", "node-0001", "node-0002"]
    proposals = {scope: [f"{scope}-{i}" for i in range(3)] for scope in scopes}
    backend = FixtureBackend(proposals, reject=[f"{rejection_scope}-0"])
    generator = Generator([(True, request().candidate_solution)])
    monkeypatch.setattr(cli, "CodexGenerator", lambda **kwargs: generator)
    monkeypatch.setattr(cli, "CodexBackend", lambda **kwargs: backend)
    run = tmp_path / "run"
    args = [
        "--problem",
        str(problem(tmp_path)),
        "--run-dir",
        str(run),
        "--max-rounds",
        "1",
        "--workers",
        "2",
    ]
    if no_early_stop:
        args.append("--no-early-stop")

    assert cli.main(args) == 3  # Rejection continues generation until the round budget.
    (report_path,) = run.glob("reviews/*/report.json")
    report = read_json(report_path)
    assert report["decision"] == "REJECT" and report["run_status"] == "completed"
    assert report["coverage_complete"] is no_early_stop
    assert report["termination_reason"] == (
        "all_nodes_reviewed" if no_early_stop else "audited_rejection"
    )
    assert backend.cancelled.is_set() is not no_early_stop
    reviewed_scopes = [
        p["scope"]["scope_id"] for role, p in backend.calls if role == "proposer"
    ]
    if no_early_stop:
        assert set(reviewed_scopes) == set(scopes)
    else:
        # Proposers may already be in flight when another scope rejects. No new
        # calls may be dispatched after the independently audited rejection.
        assert {"root", rejection_scope} <= set(reviewed_scopes) <= set(scopes)
        events = [json.loads(line) for line in (report_path.parent / "trajectory.jsonl").read_text().splitlines()]
        stop_index = next(i for i, event in enumerate(events) if event["event"] == "early_stop")
        assert not any(e["event"] == "job_dispatched" for e in events[stop_index + 1:])
    if no_early_stop:
        expected_targets = {target for targets in proposals.values() for target in targets}
        for role in ("issue_validator", "feedback_auditor"):
            assert {
                p["proposed_issue"]["target"] for r, p in backend.calls if r == role
            } == expected_targets
        assert {f["text"] for f in report["feedback"]} == {
            f"Audited fixture: {target}" for target in expected_targets
        }
    assert not (run / "handoff.json").exists()
    frozen = read_json(run / "manifest.json")["provenance"]["verifier"]["config"]
    assert frozen["early_stop_on_reject"] is not no_early_stop

    # Identical resume uses the completed review; changing the stop policy is refused.
    calls = len(backend.calls)
    assert cli.main(args) == 3
    changed_args = args[:-1] if no_early_stop else [*args, "--no-early-stop"]
    assert cli.main(changed_args) == 1
    assert len(backend.calls) == calls and len(generator.calls) == 1


def test_cli_rejects_root_only_stop_flag_before_setup(tmp_path, monkeypatch, capsys):
    def unexpected_setup(**kwargs):
        pytest.fail("Invalid options must fail before backend setup")

    monkeypatch.setattr(cli, "CodexGenerator", unexpected_setup)
    monkeypatch.setattr(cli, "CodexBackend", unexpected_setup)
    run = tmp_path / "run"
    assert cli.main(["--run-dir", str(run), "--root-only", "--no-early-stop"]) == 1
    assert "--no-early-stop cannot be combined with --root-only" in capsys.readouterr().err
    assert not run.exists()
