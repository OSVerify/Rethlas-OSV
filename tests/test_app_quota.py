"""Offline provider-error, stop-scheduling, and manual-resume regressions."""

from __future__ import annotations

import json
import os
import sys

import pytest
from osverifier.quota import QuotaExhausted
from osverifier.storage import read_json
from osverifier.workflow import DecomposingVerifier, WorkflowConfig
from pydantic import BaseModel
from test_rethlas import Generator, Reviewer, problem
from workflow_fixtures import FixtureBackend, request

from rethlas_osv.generator import CodexGenerator
from rethlas_osv.runner import Experiment, LoopConfig


@pytest.fixture
def fake_quota_codex(tmp_path):
    path = tmp_path / "fake-codex"
    path.write_text(
        f"#!{sys.executable}\n"
        + """
import json, os, sys, time
from pathlib import Path
if "--version" in sys.argv:
    print("codex-cli quota-fixture")
    raise SystemExit(0)
args = sys.argv
sys.stdin.read()
control = json.loads(Path(__file__).with_suffix(".json").read_text())
if "--cd" in args:
    workspace = Path(args[args.index("--cd")+1])
    payload = json.loads((workspace / "input.json").read_text())
    behavior = payload.get("behavior", control["behavior"])
    output = {"ok": True}
    thread = "verifier-session"
else:
    workspace = Path.cwd()
    behavior = control["behavior"]
    output = {"candidate_ready": True, "progress": "Fixture complete"}
    (workspace / "results/blueprint.md").write_text("Persisted candidate")
    thread = args[args.index("resume")+1] if "resume" in args else "generator-session"
log = Path(__file__).with_suffix(".calls")
previous = log.read_text().splitlines() if log.exists() else []
with log.open("a") as stream:
    stream.write(json.dumps({"behavior": behavior, "pid": os.getpid()})+"\\n")
print(json.dumps({"type":"thread.started", "thread_id":thread}),flush=True)
if behavior == "sleep":
    time.sleep(60)
if behavior in ("quota", "quota_hang", "quota_zero"):
    print(json.dumps({"type":"turn.failed", "error":{"code":"usage_limit_reached","message":"You've hit your usage limit."}}),flush=True)
    if behavior == "quota_hang":
        time.sleep(60)
    if behavior != "quota_zero":
        raise SystemExit(1)
elif behavior == "stderr_quota":
    print("ERROR: You exceeded your current quota, please check your plan and billing details.",file=sys.stderr,flush=True)
    raise SystemExit(1)
elif behavior == "temporary_once" and not previous:
    print(json.dumps({"type":"error", "message":"429 Too Many Requests"}),flush=True)
    raise SystemExit(1)
elif behavior == "quoted_quota":
    print(json.dumps({"type":"item.completed", "item":{"type":"agent_message","text":"You've hit your usage limit"}}),flush=True)
Path(args[args.index("--output-last-message")+1]).write_text(json.dumps(output))
print(json.dumps({"type":"turn.completed", "usage":{"input_tokens":0,"output_tokens":0}}),flush=True)
"""
    )
    path.chmod(0o755)
    path.with_suffix(".json").write_text(json.dumps({"behavior": "quota"}))
    return path


def behavior(binary, value):
    binary.with_suffix(".json").write_text(json.dumps({"behavior": value}))


def calls(binary):
    path = binary.with_suffix(".calls")
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class Output(BaseModel):
    ok: bool










def test_generator_quota_stops_outer_loop_and_manual_resume_reuses_session(
    fake_quota_codex, tmp_path
):
    question = problem(tmp_path)
    reviewer = Reviewer(["READY_FOR_HUMAN"])
    run = tmp_path / "run"

    def experiment():
        return Experiment(
            CodexGenerator(model="fixture", codex_bin=str(fake_quota_codex), agent_depth=0),
            reviewer,
            LoopConfig(2),
        )

    first = experiment().run(question, run)
    assert first.status == "incomplete" and not reviewer.calls
    assert read_json(run / "interruption.json")["phase"] == "generation"
    assert not (run / "handoff.json").exists()
    assert len(calls(fake_quota_codex)) == 1
    behavior(fake_quota_codex, "success")
    second = experiment().run(question, run)
    assert second.status == "ready_for_human"
    argv = read_json(run / "rounds/0001/attempt-0002/command.json")["argv"]
    assert argv[1:4] == ["exec", "resume", "generator-session"]
    assert not (run / "interruption.json").exists()
    assert (run / "rounds/0001/attempt-0001/interruption.json").exists()


def test_generation_without_deadline_finishes_normally_and_explicit_timeout_still_works(
    fake_quota_codex, tmp_path
):
    behavior(fake_quota_codex, "success")
    g = CodexGenerator(model="fixture", codex_bin=str(fake_quota_codex), agent_depth=0)
    assert g.identity["timeout_seconds"] is None
    result = Experiment(g, Reviewer(["READY_FOR_HUMAN"]), LoopConfig(1)).run(
        problem(tmp_path), tmp_path / "unlimited"
    )
    assert result.status == "ready_for_human", result.error
    behavior(fake_quota_codex, "sleep")
    limited = CodexGenerator(
        model="fixture", codex_bin=str(fake_quota_codex), timeout_seconds=0.3, agent_depth=0
    )
    result = Experiment(limited, Reviewer([]), LoopConfig(1)).run(
        problem(tmp_path), tmp_path / "limited"
    )
    assert result.status == "incomplete" and "TimeoutExpired" in result.error
    assert not (tmp_path / "limited/interruption.json").exists()
    with pytest.raises(ProcessLookupError):
        os.kill(calls(fake_quota_codex)[-1]["pid"], 0)




@pytest.mark.parametrize(
    "role",
    [
        "proposer",
        "decomposer",
        "deduplicator",
        "duplicate_auditor",
        "issue_validator",
        "feedback_auditor",
    ],
)
def test_decomposed_quota_stops_at_each_role_then_resumes_without_regeneration(tmp_path, role):
    class Backend(FixtureBackend):
        exhausted = True
        quota_calls = 0

        def generate(self, stage, payload, schema, attempt_dir):
            if self.exhausted and stage == role:
                self.quota_calls += 1
                raise QuotaExhausted("quota fixture")
            return super().generate(stage, payload, schema, attempt_dir)

    backend = Backend({"root": ["first", "second"], "node-0001": ["duplicate"]}, dedup=True)
    backend.veto = {"first", "second", "duplicate"}
    # Overturn all allegations, so successful recovery can reach handoff in one round.
    original_generate = backend.generate

    def generate(stage, payload, schema, attempt_dir):
        output = original_generate(stage, payload, schema, attempt_dir)
        if stage == "issue_validator":
            output.update(
                assessment="not_real",
                rejection_supported=False,
                completion_effect="none",
                feedback_candidates=[],
            )
        if stage == "feedback_auditor":
            output.update(issue_review_assessment="upheld")
        return output

    backend.generate = generate
    question = problem(tmp_path)
    generator = Generator([(True, request().candidate_solution)])
    verifier = DecomposingVerifier(backend, WorkflowConfig(workers=1, max_attempts=3))
    run = tmp_path / "run"
    first = Experiment(generator, verifier, LoopConfig(2)).run(question, run)
    assert first.status == "incomplete" and backend.quota_calls == 1
    assert not (run / "handoff.json").exists()
    assert len(generator.calls) == 1
    assert read_json(run / "interruption.json")["phase"] == "verification"
    cached = {p: p.read_bytes() for p in run.glob("reviews/*/jobs/*/result.json")}
    before = len(backend.calls)
    backend.exhausted = False
    second = Experiment(generator, verifier, LoopConfig(2)).run(question, run)
    assert second.status == "ready_for_human" and len(generator.calls) == 1
    assert backend.calls[before][0] == role
    assert all(p.read_bytes() == contents for p, contents in cached.items())
    assert not (run / "interruption.json").exists()




