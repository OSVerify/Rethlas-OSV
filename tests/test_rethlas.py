from __future__ import annotations

import asyncio
import json

import pytest
from osverifier.models import ReleasedFeedback, ReviewReport, SourceSpan
from osverifier.pipeline import RunConfig
from osverifier.storage import FrozenRunMismatch, atomic_json, atomic_text, read_json

from rethlas_osv.contracts import GenerationTurn, generator_feedback
from rethlas_osv.runner import Experiment, LoopConfig


class Generator:
    identity = {"kind": "scripted_generator", "model": "fixture", "reasoning_effort": "xhigh"}

    def __init__(self, turns):
        self.turns, self.calls = list(turns), []

    def generate(self, workspace, attempt, prompt, session_id, search):
        self.calls.append({"prompt": prompt, "session_id": session_id, "search": search})
        attempt.mkdir(parents=True)
        ready, proof = self.turns.pop(0)
        atomic_text(workspace / "problem.md", "GENERATOR CHANGED THE QUESTION")
        atomic_text(workspace / "results/blueprint_verified.md", "FORGED SUCCESS")
        atomic_text(workspace / "results/blueprint.md", proof)
        return GenerationTurn(candidate_ready=ready, progress="Fixture progress"), "same-session"


class Reviewer:
    backend = type("Backend", (), {"identity": {"kind": "scripted_verifier"}})()
    config = RunConfig()

    def __init__(self, decisions):
        self.decisions, self.calls, self.references = list(decisions), [], []

    def verify(self, request, directory, references=None):
        self.calls.append(request)
        self.references.append(references)
        action = self.decisions.pop(0)
        feedback = []
        if action in ("REVISE", "REJECT"):
            feedback = [
                ReleasedFeedback(
                    issue_id="I1",
                    feedback_id="F1",
                    kind="diagnosis",
                    text="Exact audited text: α ≠ β.\nDo not paraphrase.",
                    source_spans=[
                        SourceSpan(document="candidate_solution", start_line=1, end_line=1)
                    ],
                    audit_artifact="jobs/audit/result.json",
                )
            ]
        report = ReviewReport(
            request_id=request.request_id,
            mode=request.mode,
            decision=action,
            run_status="incomplete" if action is None else "completed",
            backend_kind="scripted_verifier",
            feedback=feedback,
        )
        atomic_json(directory / "request.json", request.model_dump(mode="json"))
        atomic_json(directory / "report.json", report.model_dump(mode="json"))
        atomic_json(directory / "diagnostics.json", {"unaudited": "NEVER FORWARD THIS"})
        return report


def problem(tmp_path):
    p = tmp_path / "input.md"
    p.write_text("Original complete question.\n")
    return p


@pytest.mark.parametrize("first_decision", ["REVISE", "REJECT"])
def test_revision_loop_preserves_question_feedback_session_and_exact_handoff(
    tmp_path, first_decision
):
    p = problem(tmp_path)
    generator = Generator([(True, "First candidate"), (True, "Revised candidate")])
    reviewer = Reviewer([first_decision, "READY_FOR_HUMAN"])
    run = tmp_path / "run"
    experiment = Experiment(generator, reviewer, LoopConfig(2))
    result = experiment.run(p, run)
    assert result.status == "ready_for_human" and result.human_confirmed is False
    assert [c.question for c in reviewer.calls] == [p.read_text(), p.read_text()]
    assert generator.calls[1]["session_id"] == "same-session"
    assert [c["search"] for c in generator.calls] == ["live", "disabled"]
    assert "NEVER FORWARD THIS" not in generator.calls[1]["prompt"]
    released = read_json(run / "rounds/0001/feedback.json")
    assert released == read_json(run / "generator/feedback.json")
    assert released["feedback"][0]["text"] == "Exact audited text: α ≠ β.\nDo not paraphrase."
    assert (run / "blueprint_ready_for_human.md").read_text() == "Revised candidate"
    # A finished run validates receipts and launches nothing on resume.
    assert experiment.run(p, run) == result
    assert len(generator.calls) == len(reviewer.calls) == 2


def test_partial_generation_is_reviewed_and_budget_can_extend(tmp_path):
    p = problem(tmp_path)
    g, v = (
        Generator([(False, "Only partial progress"), (True, "Full candidate")]),
        Reviewer(["PASS", "PASS", "READY_FOR_HUMAN"]),
    )
    run = tmp_path / "run"
    result = Experiment(g, v, LoopConfig(1)).run(p, run)
    assert result.status == "budget_exhausted" and v.calls[0].mode == "partial"
    assert result.last_decision == "PASS"
    assert not (run / "handoff.json").exists()
    result = Experiment(g, v, LoopConfig(2)).run(p, run)
    assert result.status == "ready_for_human" and len(g.calls) == 2
    assert g.calls[1]["session_id"] == "same-session"


def test_incomplete_verification_resumes_without_regeneration(tmp_path):
    p = problem(tmp_path)
    g, v = Generator([(True, "Candidate")]), Reviewer([None, "READY_FOR_HUMAN"])
    run = tmp_path / "run"
    experiment = Experiment(g, v, LoopConfig(2))
    assert experiment.run(p, run).status == "incomplete"
    assert not (run / "rounds/0001/feedback.json").exists()
    assert experiment.run(p, run).status == "ready_for_human"
    assert len(g.calls) == 1 and len(v.calls) == 2


def test_generator_stop_file_does_not_bypass_revise(tmp_path):
    p = problem(tmp_path)
    run = tmp_path / "run"
    result = Experiment(Generator([(True, "Candidate")]), Reviewer(["REVISE"]), LoopConfig(1)).run(
        p, run
    )
    assert result.status == "budget_exhausted"
    assert (run / "generator/results/blueprint_verified.md").exists()
    assert not (run / "handoff.json").exists()


def test_handoff_tamper_is_detected(tmp_path):
    p = problem(tmp_path)
    run = tmp_path / "run"
    experiment = Experiment(Generator([(True, "Candidate")]), Reviewer(["READY_FOR_HUMAN"]))
    experiment.run(p, run)
    (run / "blueprint_ready_for_human.md").write_text("Unreviewed candidate")
    with pytest.raises(FrozenRunMismatch):
        experiment.run(p, run)


def test_changed_question_refuses_resume(tmp_path):
    p = problem(tmp_path)
    run = tmp_path / "run"
    experiment = Experiment(Generator([(False, "Partial")]), Reviewer(["PASS"]), LoopConfig(1))
    experiment.run(p, run)
    p.write_text("Another question")
    with pytest.raises(FrozenRunMismatch):
        experiment.run(p, run)


def test_prepare_only_has_no_generation_or_verification_calls(tmp_path):
    g, v = Generator([]), Reviewer([])
    result = Experiment(g, v).run(problem(tmp_path), tmp_path / "run", prepare_only=True)
    assert result.status == "running" and not g.calls and not v.calls


def test_incomplete_report_cannot_become_generator_feedback():
    report = ReviewReport(
        request_id="x", decision=None, run_status="incomplete", backend_kind="fixture", feedback=[]
    )
    with pytest.raises(ValueError):
        generator_feedback(report)


def test_memory_tool_protocol_and_search_disabled(tmp_path):
    fastmcp = pytest.importorskip("fastmcp")
    from rethlas_osv.memory_server import build_app

    async def check():
        async with fastmcp.Client(build_app(tmp_path, allow_search=False)) as client:
            names = {tool.name for tool in await client.list_tools()}
            assert names == {"memory_init", "memory_append", "memory_search", "branch_update"}
            initialized = await client.call_tool("memory_init", {"problem_id": "problem"})
            assert initialized.data["problem_id"] == "problem"
            await client.call_tool(
                "memory_append",
                {
                    "problem_id": "problem",
                    "channel": "proof_steps",
                    "record": {"text": "cyclic group lemma"},
                },
            )
            result = await client.call_tool(
                "memory_search",
                {"problem_id": "problem", "query": "cyclic", "channels": ["proof_steps"]},
            )
            assert result.data["count"] == 1

    asyncio.run(check())


def test_generator_commands_use_same_session_and_local_mcp(tmp_path):
    from rethlas_osv.generator import CodexGenerator

    # Bypass binary discovery only for this argument-construction unit test.
    generator = object.__new__(CodexGenerator)
    generator.transport = type(
        "Transport",
        (),
        {
            "codex_bin": "codex",
            "model": "fixture",
            "reasoning_effort": "xhigh",
            "connection": type("Connection", (), {"arguments": []})(),
        },
    )()
    generator.agent_threads, generator.agent_depth = 4, 2
    command = generator.command(tmp_path, tmp_path / "attempt", "session-123", "disabled")
    assert command[:4] == ["codex", "exec", "resume", "session-123"]
    assert "--ignore-user-config" in command
    assert 'sandbox_mode="workspace-write"' in command
    assert 'mcp_servers.rethlas.env.RETHLAS_ALLOW_SEARCH="0"' in command
    assert 'mcp_servers.rethlas.default_tools_approval_mode="approve"' in command
    # Codex spawns the server without a locale; the compiled bundle starts it slowly.
    assert 'mcp_servers.rethlas.env.PYTHONUTF8="1"' in command
    assert "mcp_servers.rethlas.startup_timeout_sec=120" in command
    assert "--dangerously-bypass-approvals-and-sandbox" not in command


def test_tool_annotations_stay_evaluated_for_the_compiled_bundle(tmp_path):
    """String annotations make fastmcp rebuild each tool with ``types.FunctionType``,
    which fails on Nuitka-compiled closures ("expected cell, found compiled_cell") and
    kills the memory server inside the desktop app before it can answer Codex."""
    import typing

    pytest.importorskip("fastmcp")
    from rethlas_osv.memory_server import build_app

    tools = asyncio.run(build_app(tmp_path, allow_search=True).get_tools())
    assert set(tools) == {
        "memory_init", "memory_append", "memory_search", "branch_update", "search_arxiv_theorems"
    }
    for name, tool in tools.items():
        assert not any(isinstance(v, str) for v in tool.fn.__annotations__.values()), name
        assert typing.get_type_hints(tool.fn, include_extras=True) == tool.fn.__annotations__, name


def test_memory_server_process_handshakes_without_a_locale(tmp_path):
    """The real server process, in the minimal environment Codex gives MCP servers."""
    import os
    import subprocess
    import sys

    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "PYTHONUTF8": "1"}
    process = subprocess.Popen(
        [sys.executable, "-m", "rethlas_osv.memory_server", "--workspace", str(tmp_path / "ws")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )

    def send(message):
        process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        process.stdin.flush()
        return json.loads(process.stdout.readline())

    try:
        hello = send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"}}})
        assert "result" in hello, hello
        process.stdin.write(b'{"jsonrpc": "2.0", "method": "notifications/initialized"}\n')
        tools = send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert {"memory_init", "memory_append", "memory_search", "branch_update"} <= {
            t["name"] for t in tools["result"]["tools"]
        }
        note = "∀ε>0 ∃δ>0: 测试"
        appended = send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "memory_append", "arguments": {
                "problem_id": "p", "channel": "events", "record": {"note": note}}}})
        assert not appended["result"].get("isError"), appended
        assert note in json.loads(appended["result"]["content"][0]["text"])["entry"]["record"]["note"]
        stored = (tmp_path / "ws/memory/p/events.jsonl").read_text(encoding="utf-8")
        assert note in stored
    finally:
        process.stdin.close()
        assert process.wait(timeout=30) == 0, process.stderr.read().decode("utf-8", "replace")


def test_non_utf8_problem_file_is_reported_clearly(tmp_path):
    p = tmp_path / "latin1.md"
    p.write_bytes("Soit \u00e9 fix\u00e9 \u00e0 z\u00e9ro.\n".encode("latin-1"))
    experiment = Experiment(Generator([]), Reviewer([]), LoopConfig(1))
    with pytest.raises(ValueError, match="not UTF-8 text"):
        experiment.run(p, tmp_path / "run")
    assert not (tmp_path / "run/manifest.json").exists()


def test_shell_network_access_follows_the_turn_search_setting(tmp_path):
    from rethlas_osv.generator import CodexGenerator

    generator = object.__new__(CodexGenerator)
    generator.transport = type(
        "Transport",
        (),
        {
            "codex_bin": "codex",
            "model": "fixture",
            "reasoning_effort": "xhigh",
            "connection": type("Connection", (), {"arguments": []})(),
        },
    )()
    generator.agent_threads, generator.agent_depth = 4, 2
    for search, network in (("disabled", "false"), ("live", "true")):
        command = generator.command(tmp_path, tmp_path / "attempt", None, search)
        assert f"sandbox_workspace_write.network_access={network}" in command
        assert f'web_search="{search}"' in command


@pytest.mark.parametrize("decision", ["PASS", "REVISE", "REJECT"])
def test_partial_feedback_continues_same_problem_without_handoff(tmp_path, decision):
    g = Generator([(False, "Partial argument"), (True, "Complete argument")])
    v = Reviewer([decision, "READY_FOR_HUMAN"])
    result = Experiment(g, v, LoopConfig(2)).run(problem(tmp_path), tmp_path / "run")
    assert result.status == "ready_for_human", result.error
    assert [c.mode for c in v.calls] == ["partial", "full_solution"]
    assert v.calls[0].question == v.calls[1].question
    assert '"mode": "partial"' in g.calls[1]["prompt"]
    assert f'"decision": "{decision}"' in g.calls[1]["prompt"]
    if decision != "PASS":
        assert "Exact audited text" in g.calls[1]["prompt"]


def test_partial_interruption_resumes_before_generation_continues(tmp_path):
    g = Generator([(False, "Partial"), (True, "Complete")])
    v = Reviewer([None, "PASS", "READY_FOR_HUMAN"])
    p, run = problem(tmp_path), tmp_path / "run"
    experiment = Experiment(g, v, LoopConfig(2))
    assert experiment.run(p, run).status == "incomplete"
    assert len(g.calls) == 1
    assert experiment.run(p, run).status == "ready_for_human"
    assert len(g.calls) == 2
    assert [c.mode for c in v.calls] == ["partial", "partial", "full_solution"]


def test_empty_partial_draft_cannot_skip_verification(tmp_path):
    g, v = Generator([(False, " ")]), Reviewer([])
    result = Experiment(g, v).run(problem(tmp_path), tmp_path / "run")
    assert result.status == "incomplete" and "empty" in result.error
    assert not v.calls


def test_partial_report_cannot_masquerade_as_full_handoff(tmp_path):
    class WrongModeReviewer(Reviewer):
        def verify(self, request, directory):
            return super().verify(request.model_copy(update={"mode": "full_solution"}), directory)

    g, v = Generator([(False, "Partial")]), WrongModeReviewer(["READY_FOR_HUMAN"])
    run = tmp_path / "run"
    result = Experiment(g, v).run(problem(tmp_path), run)
    assert result.status == "incomplete" and "mode" in result.error
    assert not (run / "handoff.json").exists()


def test_workspace_restore_replaces_generator_links_instead_of_writing_through(tmp_path):
    import os
    import shutil

    from rethlas_osv.runner import ROOT

    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "user-file.txt"
    victim.write_text("USER DATA\n")
    linked_dirs = [outside / "skills", outside / "agents"]
    for path in linked_dirs:
        path.mkdir()
    p = problem(tmp_path)
    refs = tmp_path / "input.refs"
    refs.mkdir()
    (refs / "notes.md").write_text("Reference notes\n")

    class LinkingGenerator(Generator):
        def generate(self, workspace, attempt, prompt, session_id, search):
            turn = super().generate(workspace, attempt, prompt, session_id, search)
            # A workspace-write sandbox permits creating these links inside the workspace.
            for name in ("AGENTS.md", "problem.md", "references/notes.md"):
                (workspace / name).unlink()
                os.symlink(victim, workspace / name)
            for name, target in zip((".agents/skills/query-memory", ".codex/agents"), linked_dirs):
                shutil.rmtree(workspace / name)
                os.symlink(target, workspace / name)
            draft = workspace / "drafts/blueprint.md"
            draft.parent.mkdir()
            (workspace / "results/blueprint.md").rename(draft)
            os.symlink("../drafts/blueprint.md", workspace / "results/blueprint.md")
            return turn

    run = tmp_path / "run"
    g, v = LinkingGenerator([(True, "First")]), Reviewer(["REVISE", "REVISE"])
    assert Experiment(g, v, LoopConfig(1)).run(p, run, refs).status == "budget_exhausted"
    assert v.references == [run / "inputs/references"]
    workspace = run / "generator"
    # The post-review draft write replaced the in-workspace link.
    assert not (workspace / "results/blueprint.md").is_symlink()
    assert (workspace / "drafts/blueprint.md").read_text() == "First"
    # Rerunning restores maintained files without writing through any link.
    assert Experiment(g, v, LoopConfig(1)).run(p, run, refs).status == "budget_exhausted"
    assert len(g.calls) == 1
    assert victim.read_text() == "USER DATA\n"
    assert all(not any(path.iterdir()) for path in linked_dirs)
    for name, source in (
        ("AGENTS.md", ROOT / "generation-AGENTS.md"),
        ("references/notes.md", refs / "notes.md"),
        (".agents/skills/query-memory/SKILL.md",
         ROOT / "vendor/generation/.agents/skills/query-memory/SKILL.md"),
    ):
        path = workspace / name
        assert not path.is_symlink() and path.read_bytes() == source.read_bytes()
    assert (workspace / ".codex/agents/subgoal-prover.toml").is_file()
    assert not (workspace / ".codex/agents").is_symlink()


def test_memory_server_refuses_links_out_of_the_workspace(tmp_path):
    import os

    fastmcp = pytest.importorskip("fastmcp")
    from rethlas_osv.memory_server import build_app

    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "user-file.txt"
    victim.write_text("USER DATA\n")

    async def check(workspace, expect_error):
        async with fastmcp.Client(build_app(workspace, allow_search=False)) as client:
            record = {"problem_id": "problem", "channel": "proof_steps", "record": {"t": 1}}
            if expect_error:
                with pytest.raises(Exception, match="memory"):
                    await client.call_tool("memory_append", record)
            else:
                await client.call_tool("memory_append", record)

    # A symbolic link at the memory root, at a channel file, and a hard link.
    linked_root = tmp_path / "linked-root"
    (linked_root).mkdir()
    os.symlink(outside, linked_root / "memory")
    linked_file = tmp_path / "linked-file"
    (linked_file / "memory/problem").mkdir(parents=True)
    os.symlink(victim, linked_file / "memory/problem/proof_steps.jsonl")
    hard_link = tmp_path / "hard-link"
    (hard_link / "memory/problem").mkdir(parents=True)
    os.link(victim, hard_link / "memory/problem/events.jsonl")
    for workspace in (linked_root, linked_file, hard_link):
        asyncio.run(check(workspace, expect_error=True))
    assert victim.read_text() == "USER DATA\n"
    assert sorted(path.name for path in outside.iterdir()) == ["user-file.txt"]
    # Ordinary memory still works.
    asyncio.run(check(tmp_path / "plain", expect_error=False))
    assert (tmp_path / "plain/memory/problem/proof_steps.jsonl").read_text().count("\n") == 1


def test_theorem_search_exposes_only_query_and_result_count(tmp_path, monkeypatch):
    fastmcp = pytest.importorskip("fastmcp")
    requests = pytest.importorskip("requests")
    from rethlas_osv.memory_server import build_app

    posted = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return [{"title": "T", "theorem": "Every group of prime order is cyclic."}]

    def post(url, json, timeout):
        posted.append((url, json, timeout))
        return Response()

    monkeypatch.setattr(requests, "post", post)

    async def check():
        async with fastmcp.Client(build_app(tmp_path, allow_search=True)) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            schema = tools["search_arxiv_theorems"].inputSchema
            assert set(schema["properties"]) == {"query", "num_results"}
            with pytest.raises(Exception):
                await client.call_tool(
                    "search_arxiv_theorems",
                    {"query": "prime order", "endpoint": "http://127.0.0.1:1/steal"},
                )
            result = await client.call_tool("search_arxiv_theorems", {"query": "prime order"})
            assert result.data["count"] == 1

    asyncio.run(check())
    assert [(url, timeout) for url, _, timeout in posted] == [
        ("https://leansearch.net/thm/search", 30)
    ]


def test_rejected_turn_output_reason_reaches_the_resumed_turn(tmp_path):
    g = Generator([(True, ""), (True, "Now complete")])
    v = Reviewer(["READY_FOR_HUMAN"])
    p, run = problem(tmp_path), tmp_path / "run"
    experiment = Experiment(g, v, LoopConfig(1))
    assert experiment.run(p, run).status == "incomplete"
    reason = "results/blueprint.md was empty"
    assert read_json(run / "rounds/0001/attempt-0001/output-rejected.json") == {"reason": reason}
    events = [json.loads(line) for line in (run / "trajectory.jsonl").read_text().splitlines()]
    assert any(
        row["event"] == "generation_interrupted" and row["reason"] == "invalid_output"
        for row in events
    )
    assert experiment.run(p, run).status == "ready_for_human"
    resumed = g.calls[1]["prompt"]
    assert resumed.startswith("Resume the interrupted generation turn.")
    assert f"without a valid submission: {reason}." in resumed
    assert [c.candidate_solution for c in v.calls] == ["Now complete"]


def test_schema_invalid_final_response_is_a_rejected_turn_output(tmp_path):
    import sys

    from rethlas_osv.contracts import InvalidGenerationOutput
    from rethlas_osv.generator import CodexGenerator

    binary = tmp_path / "codex"
    binary.write_text(
        f"#!{sys.executable}\n"
        + """
import json, sys
from pathlib import Path
if "--version" in sys.argv:
    print("codex-cli fixture"); raise SystemExit(0)
sys.stdin.read()
print(json.dumps({"type": "thread.started", "thread_id": "session"}), flush=True)
out = Path(sys.argv[sys.argv.index("--output-last-message") + 1])
out.write_text(json.dumps({"candidate_ready": True}))
"""
    )
    binary.chmod(0o755)
    generator = CodexGenerator(model="fixture", codex_bin=str(binary), agent_depth=0)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(InvalidGenerationOutput, match="required schema"):
        generator.generate(workspace, tmp_path / "attempt", "prompt", None, "disabled")


def test_generation_timeout_records_no_connection_overrides(tmp_path):
    import sys

    from rethlas_osv.generator import CodexGenerator

    home = tmp_path / "account"
    home.mkdir()
    (home / "config.toml").write_text(
        'model_provider = "proxy"\n[model_providers.proxy]\nname = "proxy"\n'
        'base_url = "https://proxy.example/v1"\n'
        'http_headers = { "Authorization" = "Bearer SECRET-TOKEN" }\n'
    )
    binary = tmp_path / "codex"
    binary.write_text(
        f"#!{sys.executable}\nimport sys, time\n"
        "if '--version' in sys.argv:\n    print('codex-cli fixture'); raise SystemExit(0)\n"
        "sys.stdin.read(); time.sleep(30)\n"
    )
    binary.chmod(0o755)
    generator = CodexGenerator(
        model="fixture", codex_bin=str(binary), codex_home=home, timeout_seconds=0.5, agent_depth=0
    )
    rows, run = [], tmp_path / "run"
    result = Experiment(generator, Reviewer([]), LoopConfig(1), rows.append).run(
        problem(tmp_path), run
    )
    assert result.status == "incomplete" and "TimeoutExpired" in result.error
    persisted = [path.read_text() for path in run.rglob("*") if path.is_file()]
    assert not any("SECRET-TOKEN" in text or "proxy.example" in text for text in persisted)
    assert "SECRET-TOKEN" not in json.dumps(rows)
