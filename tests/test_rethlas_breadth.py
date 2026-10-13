"""Offline breadth scheduling, branch isolation, and interrupted-wave recovery."""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time

import pytest
from osverifier.backend import CodexBackend
from osverifier.quota import QuotaExhausted, record_quota
from osverifier.storage import FrozenRunMismatch, atomic_json, atomic_text, read_json
from osverifier.workflow import DecomposingVerifier
from test_rethlas import Reviewer, problem

from rethlas_osv.cli import parser
from rethlas_osv.contracts import GenerationTurn, read_report
from rethlas_osv.generator import CodexGenerator
from rethlas_osv.runner import Experiment, LoopConfig


class ParallelGenerator:
    identity = {"kind": "parallel_fixture", "model": "fixture", "reasoning_effort": "xhigh"}

    def __init__(self, width, *, partial=(), identical=False):
        self.barrier = threading.Barrier(width, timeout=5)
        self.calls, self.partial, self.identical = [], partial, identical

    def generate(self, workspace, attempt, prompt, session_id, search):
        branch, round_number = int(workspace.parent.name), int(attempt.parent.name)
        self.calls.append(
            {
                "branch": branch,
                "round": round_number,
                "session": session_id,
                "prompt": prompt,
                "search": search,
            }
        )
        # A serial implementation cannot pass this barrier.
        self.barrier.wait()
        text = "Shared candidate" if self.identical else f"Branch {branch}, round {round_number}"
        atomic_text(workspace / "results/blueprint.md", text)
        return GenerationTurn(
            candidate_ready=(branch, round_number) not in self.partial, progress="Fixture progress"
        ), f"session-{branch}"


class CachingReviewer(Reviewer):
    def verify(self, request, directory):
        path = directory / "report.json"
        if path.exists():
            cached = read_report(read_json(path))
            if cached.run_status == "completed":
                return cached
        report = super().verify(request, directory)
        if report.feedback:
            report.feedback[0].text = f"Audited feedback for {request.candidate_solution}."
            atomic_json(path, report.model_dump(mode="json"))
        return report


def test_parallel_wave_and_first_eligible_handoff(tmp_path):
    g = ParallelGenerator(4)
    v = CachingReviewer(["REJECT", "REVISE", "READY_FOR_HUMAN"])
    rows = []
    experiment = Experiment(g, v, LoopConfig(10, generation_branches=4), rows.append)
    p, run = problem(tmp_path), tmp_path / "run"
    result = experiment.run(p, run)
    assert result.status == "ready_for_human", result.error
    assert result.winning_branch == 3 and result.rounds_completed == 1
    assert result.candidates_generated == result.generation_turns_completed == 4
    assert len(g.calls) == 4 and len(v.calls) == 3
    assert all(call["session"] is None for call in g.calls)
    finished = [i for i, row in enumerate(rows) if row["event"] == "generation_finished"]
    first_review = next(i for i, row in enumerate(rows) if row["event"] == "verification_started")
    assert max(finished) < first_review
    assert any(row["event"] == "verification_skipped" and row["branch"] == 4 for row in rows)
    receipt = read_json(run / "handoff.json")
    assert receipt["generation_artifact"] == "branches/0003/rounds/0001/generation.json"
    assert (run / "blueprint_ready_for_human.md").read_text() == "Branch 3, round 1"
    assert not result.human_confirmed
    assert experiment.run(p, run) == result
    assert len(g.calls) == 4 and len(v.calls) == 3


def test_rounds_are_per_branch_and_feedback_and_sessions_stay_local(tmp_path):
    g, v = ParallelGenerator(3), CachingReviewer(["REVISE"] * 6)
    p, run = problem(tmp_path), tmp_path / "run"
    result = Experiment(g, v, LoopConfig(2, generation_branches=3)).run(p, run)
    assert result.status == "budget_exhausted", result.error
    assert result.rounds_completed == 2 and result.generation_turns_completed == 6
    for call in g.calls:
        branch, round_number = call["branch"], call["round"]
        assert call["search"] == ("live" if round_number == 1 else "disabled")
        assert call["session"] == (None if round_number == 1 else f"session-{branch}")
        assert f"independent search branch {branch} of 3" in call["prompt"]
        if round_number == 2:
            assert f"Audited feedback for Branch {branch}, round 1." in call["prompt"]
            assert "NEVER FORWARD THIS" not in call["prompt"]
            for other in {1, 2, 3} - {branch}:
                assert f"Audited feedback for Branch {other}" not in call["prompt"]
    # Increasing depth preserves width and all old snapshots/sessions.
    v.decisions = ["READY_FOR_HUMAN"]
    result = Experiment(g, v, LoopConfig(3, generation_branches=3)).run(p, run)
    assert result.status == "ready_for_human" and len(g.calls) == 9


def test_identical_proofs_share_cache_only_within_same_review_mode(tmp_path):
    g = ParallelGenerator(3, partial={(1, 1)}, identical=True)
    v = CachingReviewer(["PASS", "REVISE"])
    result = Experiment(g, v, LoopConfig(1, generation_branches=3)).run(
        problem(tmp_path), tmp_path / "run"
    )
    assert result.status == "budget_exhausted", result.error
    assert result.candidates_generated == 2 and result.generation_turns_completed == 3
    assert len(v.calls) == 2
    assert [c.mode for c in v.calls] == ["partial", "full_solution"]


def test_incomplete_review_resumes_pending_wave_without_regenerating(tmp_path):
    g, v = ParallelGenerator(3), CachingReviewer(["REVISE", None, "READY_FOR_HUMAN"])
    p, run = problem(tmp_path), tmp_path / "run"
    experiment = Experiment(g, v, LoopConfig(2, generation_branches=3))
    result = experiment.run(p, run)
    assert result.status == "incomplete" and result.rounds_completed == 0
    snapshots = {
        path: path.read_bytes() for path in run.glob("branches/*/rounds/*/generation.json")
    }
    result = experiment.run(p, run)
    assert result.status == "ready_for_human" and result.winning_branch == 2
    assert len(g.calls) == 3 and len(v.calls) == 3
    assert all(path.read_bytes() == data for path, data in snapshots.items())


def test_failed_later_generation_keeps_new_draft_and_completed_sibling(tmp_path):
    class InterruptedGenerator:
        identity = ParallelGenerator.identity

        def __init__(self):
            self.calls, self.failed = [], False

        def generate(self, workspace, attempt, prompt, session_id, search):
            branch, round_number = int(workspace.parent.name), int(attempt.parent.name)
            self.calls.append((branch, round_number))
            atomic_json(attempt / "events-summary.json", {"thread_ids": [f"session-{branch}"]})
            if (branch, round_number) == (1, 2):
                if not self.failed:
                    atomic_text(workspace / "results/blueprint.md", "New unfinished progress")
                    self.failed = True
                    raise RuntimeError("interrupted turn")
                assert (workspace / "results/blueprint.md").read_text() == "New unfinished progress"
                assert session_id == "session-1"
                assert "Resume the interrupted generation turn" in prompt
            atomic_text(
                workspace / "results/blueprint.md", f"Branch {branch}, round {round_number}"
            )
            return GenerationTurn(candidate_ready=True, progress="Fixture"), f"session-{branch}"

    p, run = problem(tmp_path), tmp_path / "run"
    g, v = InterruptedGenerator(), CachingReviewer(["REVISE", "REVISE", "READY_FOR_HUMAN"])
    experiment = Experiment(g, v, LoopConfig(2, generation_branches=2))
    first = experiment.run(p, run)
    assert first.status == "incomplete" and first.rounds_completed == 1
    assert first.generation_turns_completed == 3 and len(v.calls) == 2
    cached = run / "branches/0002/rounds/0002/generation.json"
    original = cached.read_bytes()
    result = experiment.run(p, run)
    assert result.status == "ready_for_human", result.error
    assert g.calls.count((1, 2)) == 2 and g.calls.count((2, 2)) == 1
    assert cached.read_bytes() == original


def test_width_is_frozen_and_prepare_only_makes_all_workspaces(tmp_path):
    p, run = problem(tmp_path), tmp_path / "run"
    g, v = ParallelGenerator(4), CachingReviewer([])
    result = Experiment(g, v, LoopConfig(2, generation_branches=4)).run(p, run, prepare_only=True)
    assert result.status == "running" and not g.calls and not v.calls
    assert len(list(run.glob("branches/*/generator/AGENTS.md"))) == 4
    with pytest.raises(FrozenRunMismatch):
        Experiment(g, v, LoopConfig(2, generation_branches=2)).run(p, run)
    assert (
        parser().parse_args(["--run-dir", "run", "--generation-branches", "4"]).generation_branches
        == 4
    )
    with pytest.raises(ValueError, match="generation_branches"):
        LoopConfig(generation_branches=0)


@pytest.fixture
def parallel_codex(tmp_path):
    binary = tmp_path / "parallel-codex"
    binary.write_text(
        f"#!{sys.executable}\n"
        + r"""
import json, os, signal, sys, time
from pathlib import Path
if "--version" in sys.argv:
    print("codex-cli parallel-fixture")
    raise SystemExit(0)
args = sys.argv
sys.stdin.read()
workspace = Path.cwd()
branch = int(workspace.parent.name)
run = workspace.parents[2]
mode = Path(__file__).with_suffix(".mode").read_text()
if mode == "stubborn":
    # A slowly exiting CLI: survives SIGTERM, so only SIGKILL ends it.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
thread = args[args.index("resume")+1] if "resume" in args else f"session-{branch}"
with Path(__file__).with_suffix(".calls").open("a") as stream:
    stream.write(json.dumps({"branch":branch,"pid":os.getpid(),"session":thread})+"\n")
print(json.dumps({"type":"thread.started","thread_id":thread}),flush=True)
(workspace / "started").write_text(str(os.getpid()))
(workspace / "results/blueprint.md").write_text(f"Draft for branch {branch}")
if mode in ("sleep", "stubborn") or (mode == "quota" and branch == 3):
    time.sleep(60)
if mode == "quota" and branch == 2:
    deadline = time.monotonic()+5
    while not ((run / "branches/0001/rounds/0001/generation.json").exists()
               and (run / "branches/0003/generator/started").exists()):
        if time.monotonic() > deadline:
            raise RuntimeError("other branches did not run concurrently")
        time.sleep(.01)
    print(json.dumps({"type":"turn.failed","error":{"code":"usage_limit_reached"}}),flush=True)
    time.sleep(60)
Path(args[args.index("--output-last-message")+1]).write_text(json.dumps(
    {"candidate_ready":True,"progress":"Fixture complete"}))
print(json.dumps({"type":"turn.completed"}),flush=True)
"""
    )
    binary.chmod(0o755)
    binary.with_suffix(".mode").write_text("quota")
    return binary


def process_calls(binary):
    path = binary.with_suffix(".calls")
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.mark.parametrize("timeout", [None, 15])
def test_generation_quota_stops_siblings_and_resumes_only_missing_branches(
    parallel_codex, tmp_path, timeout
):
    p, run = problem(tmp_path), tmp_path / "run"
    v = CachingReviewer(["REVISE", "READY_FOR_HUMAN"])

    def experiment():
        return Experiment(
            CodexGenerator(
                model="fixture",
                codex_bin=str(parallel_codex),
                timeout_seconds=timeout,
                agent_depth=0,
            ),
            v,
            LoopConfig(2, generation_branches=3),
        )

    started = time.monotonic()
    result = experiment().run(p, run)
    assert result.status == "incomplete" and "quota" in result.error.lower(), result.error
    assert time.monotonic() - started < 8
    assert not v.calls and len(process_calls(parallel_codex)) == 3
    assert result.generation_turns_completed == 1
    cached = run / "branches/0001/rounds/0001/generation.json"
    original = cached.read_bytes()
    for call in process_calls(parallel_codex):
        with pytest.raises(ProcessLookupError):
            os.kill(call["pid"], 0)
    assert read_json(run / "interruption.json")["reason"] == "quota_exhausted"
    parallel_codex.with_suffix(".mode").write_text("success")
    result = experiment().run(p, run)
    assert result.status == "ready_for_human", result.error
    assert result.winning_branch == 2 and len(process_calls(parallel_codex)) == 5
    assert cached.read_bytes() == original
    for branch in (2, 3):
        command = read_json(run / f"branches/{branch:04d}/rounds/0001/attempt-0002/command.json")
        assert command["argv"][1:4] == ["exec", "resume", f"session-{branch}"]
    assert not (run / "interruption.json").exists()


def test_verifier_quota_stops_wave_before_any_more_reviews_or_depth(tmp_path):
    class QuotaReviewer(CachingReviewer):
        def verify(self, request, directory):
            report = super().verify(request, directory)
            if report.run_status == "incomplete":
                record_quota(directory, QuotaExhausted("fixture quota"))
            return report

    g, v = ParallelGenerator(3), QuotaReviewer([None, "READY_FOR_HUMAN"])
    p, run = problem(tmp_path), tmp_path / "run"
    experiment = Experiment(g, v, LoopConfig(2, generation_branches=3))
    result = experiment.run(p, run)
    assert result.status == "incomplete" and len(g.calls) == 3 and len(v.calls) == 1
    assert read_json(run / "interruption.json")["phase"] == "verification"
    assert not list(run.glob("branches/*/rounds/0002"))
    result = experiment.run(p, run)
    assert result.status == "ready_for_human" and len(g.calls) == 3


def test_rejection_cancellation_is_scoped_to_one_candidate(parallel_codex, tmp_path, monkeypatch):
    backend = CodexBackend(model="fixture", codex_bin=str(parallel_codex))
    seen = []
    reviewer = CachingReviewer(["REJECT", "READY_FOR_HUMAN"])

    def verify(self, request, directory):
        assert not self.backend._cancelled.is_set()
        seen.append(self.backend)
        self.backend.cancel()
        return reviewer.verify(request, directory)

    monkeypatch.setattr(DecomposingVerifier, "verify", verify)
    result = Experiment(
        ParallelGenerator(2), DecomposingVerifier(backend), LoopConfig(1, generation_branches=2)
    ).run(problem(tmp_path), tmp_path / "run")
    assert result.status == "ready_for_human", result.error
    assert len(seen) == 2 and seen[0] is not seen[1] and not backend._cancelled.is_set()


def test_ctrl_c_stops_all_generation_processes(parallel_codex, tmp_path):
    parallel_codex.with_suffix(".mode").write_text("sleep")
    g = CodexGenerator(model="fixture", codex_bin=str(parallel_codex), agent_depth=0)
    p, run = problem(tmp_path), tmp_path / "run"
    cancel_wait = threading.Event()

    def interrupt():
        deadline = time.monotonic() + 5
        while not cancel_wait.wait(0.02):
            if len(process_calls(parallel_codex)) == 3 or time.monotonic() > deadline:
                os.kill(os.getpid(), signal.SIGINT)
                return

    thread = threading.Thread(target=interrupt)
    thread.start()
    try:
        with pytest.raises(KeyboardInterrupt):
            Experiment(g, CachingReviewer([]), LoopConfig(2, generation_branches=3)).run(p, run)
    finally:
        cancel_wait.set()
        thread.join(timeout=5)
        g.cancel()
    assert read_json(run / "summary.json")["status"] == "incomplete"
    assert len(process_calls(parallel_codex)) == 3
    for call in process_calls(parallel_codex):
        with pytest.raises(ProcessLookupError):
            os.kill(call["pid"], 0)
    assert len(list(run.glob("branches/*/rounds/0001/attempt-0001/events-summary.json"))) == 3


def test_repeated_ctrl_c_still_stops_every_generation_branch(parallel_codex, tmp_path, monkeypatch):
    from osverifier import backend

    monkeypatch.setattr(backend, "STOP_GRACE_SECONDS", 1.0)
    parallel_codex.with_suffix(".mode").write_text("stubborn")
    g = CodexGenerator(model="fixture", codex_bin=str(parallel_codex), agent_depth=0)
    p, run = problem(tmp_path), tmp_path / "run"
    done = threading.Event()

    def interrupt_twice():
        deadline = time.monotonic() + 5
        while len(process_calls(parallel_codex)) < 3 and time.monotonic() < deadline:
            if done.wait(0.02):
                return
        os.kill(os.getpid(), signal.SIGINT)
        # An impatient second Ctrl+C lands while the first cleanup is waiting.
        if not done.wait(0.3):
            os.kill(os.getpid(), signal.SIGINT)

    thread = threading.Thread(target=interrupt_twice)
    started = time.monotonic()
    thread.start()
    try:
        with pytest.raises(KeyboardInterrupt):
            Experiment(g, CachingReviewer([]), LoopConfig(1, generation_branches=3)).run(p, run)
    finally:
        done.set()
        thread.join(timeout=5)
    # Previously the second interrupt abandoned unsignalled branches and the
    # invocation then blocked until their 60-second turns ended.
    assert time.monotonic() - started < 15
    assert len(process_calls(parallel_codex)) == 3
    for call in process_calls(parallel_codex):
        with pytest.raises(ProcessLookupError):
            os.kill(call["pid"], 0)
