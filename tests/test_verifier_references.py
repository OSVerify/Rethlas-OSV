"""Frozen references reach the verifier, and a stuck project restarts from its draft with them.

Scripted backends establish plumbing only, never that a supplied source was judged correctly.
"""

import hashlib
import json

import pytest
from osverifier.storage import FrozenRunMismatch, read_json
from osverifier_reports.reference_tools import SuppliedReferenceTools
from test_rethlas import Generator, Reviewer, problem
from workflow_fixtures import FixtureBackend

from rethlas_osv import cli
from rethlas_osv.runner import Experiment, LoopConfig

PROOF = "".join(f"Step {i} applies Lemma 2 of Smith (1962).\n" for i in range(1, 7))
SOURCE = "Smith (1962), On sequences.\nLemma 2. Every monotone bounded sequence converges.\n"


class CitationBackend(FixtureBackend):
    """The validator settles the cited lemma only by reading a supplied copy of the source."""

    def __init__(self):
        super().__init__({"root": ["UNAUDITED: cited Lemma 2 of Smith (1962) not located"]})
        self.reads = []

    def generate_with_references(self, role, payload, schema, attempt_dir, *, reference_tools):
        tools = SuppliedReferenceTools(
            reference_tools["supplied"], reference_tools["journal"], reference_tools["work_dir"]
        )
        names = [entry["name"] for entry in payload["supplied_references"]]
        passage = tools.call("read_supplied_reference", {"reference": names[0], "query": "Lemma 2"})
        self.reads.append((role, names, passage["text_match_found"]))
        return self.settle(role, payload, schema, attempt_dir, passage["text_match_found"])

    def generate(self, role, payload, schema, attempt_dir):
        return self.settle(role, payload, schema, attempt_dir, False)

    def settle(self, role, payload, schema, attempt_dir, found):
        result = super().generate(role, payload, schema, attempt_dir)
        if role == "issue_validator":
            result.update(
                assessment="not_real" if found else "unresolved",
                impact="nonblocking" if found else "unknown",
                completion_effect="none" if found else "uncertain",
                rejection_supported=False,
                feedback_candidates=[],
            )
        return result


def references(tmp_path):
    refs = tmp_path / "refs"
    refs.mkdir()
    (refs / "smith1962.md").write_text(SOURCE)
    return refs


def run_cli(tmp_path, monkeypatch, backend, generator, run, *extra):
    monkeypatch.setattr(cli, "CodexGenerator", lambda **kwargs: generator)
    monkeypatch.setattr(cli, "CodexBackend", lambda **kwargs: backend)
    args = ["--problem", str(problem(tmp_path)), "--run-dir", str(run), "--workers", "1", *extra]
    return cli.main(args)


def tree_hashes(directory):
    return {
        str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*"))
        if path.is_file() and path.name != ".lock"
    }


def test_frozen_references_reach_every_review_and_the_generator(tmp_path, monkeypatch):
    refs, run = references(tmp_path), tmp_path / "run"
    backend = CitationBackend()
    code = run_cli(tmp_path, monkeypatch, backend, Generator([(True, PROOF)]), run,
                   "--references", str(refs), "--max-rounds", "1")
    assert code == 0 and (run / "handoff.json").exists()
    (review,) = run.glob("reviews/*")
    assert (review / "supplied-references/files/smith1962.md").read_text() == SOURCE
    provenance = read_json(review / "manifest.json")["provenance"]["supplied_references"]
    assert provenance["files"] == read_json(run / "manifest.json")["provenance"]["references"]
    assert (run / "generator/references/smith1962.md").read_text() == SOURCE
    roles = {role for role, _, _ in backend.reads}
    assert roles == {"proposer", "issue_validator", "feedback_auditor"}
    assert all(names == ["smith1962.md"] and found for _, names, found in backend.reads)


def test_stuck_project_restarts_from_its_draft_with_added_references(tmp_path, monkeypatch):
    parent, child = tmp_path / "parent", tmp_path / "child"
    # Without the source, the cited lemma stays unresolved: generation continues, no handoff.
    stuck = Generator([(True, PROOF)])
    assert run_cli(tmp_path, monkeypatch, CitationBackend(), stuck, parent, "--max-rounds", "1") == 3
    feedback = read_json(parent / "rounds/0001/feedback.json")
    assert feedback["continuation_reason"] == "verification_inconclusive"
    # Adding the references to the existing run is refused by design.
    refs = references(tmp_path)
    assert run_cli(tmp_path, monkeypatch, CitationBackend(), stuck, parent,
                   "--max-rounds", "1", "--references", str(refs)) == 1
    before = tree_hashes(parent)
    # A new run reviews the same draft, now with the source, before generating anything.
    backend, fresh = CitationBackend(), Generator([])
    code = run_cli(tmp_path, monkeypatch, backend, fresh, child, "--max-rounds", "3",
                   "--references", str(refs), "--seed-from", str(parent))
    assert code == 0 and fresh.calls == []
    assert tree_hashes(parent) == before
    handoff = read_json(child / "handoff.json")
    assert (child / handoff["proof_artifact"]).read_text() == PROOF
    assert handoff["generation_artifact"] == "rounds/0001/generation.json"
    assert handoff["generation_turns_completed"] == 0 and handoff["rounds_completed"] == 1
    seed = read_json(child / "manifest.json")["provenance"]["seed"]
    assert seed == {
        "run_dir": str(parent.resolve()),
        "manifest_sha256": before["manifest.json"],
        "branch": 1,
        "generation_artifact": "rounds/0001/generation.json",
        "generation_sha256": before["rounds/0001/generation.json"],
        "candidate_sha256": hashlib.sha256(PROOF.encode()).hexdigest(),
    }
    assert (child / "inputs/seed/generation.json").read_bytes() == (parent / "rounds/0001/generation.json").read_bytes()
    events = [json.loads(line) for line in (child / "trajectory.jsonl").read_text().splitlines()]
    assert [e["parent_run"] for e in events if e["event"] == "seeded_from_run"] == [str(parent.resolve())]
    # The handoff replays from frozen artifacts, with or without naming the parent again.
    for extra in ((), ("--seed-from", str(parent))):
        again = CitationBackend()
        assert run_cli(tmp_path, monkeypatch, again, Generator([]), child, "--max-rounds", "3",
                       "--references", str(refs), *extra) == 0
        assert again.calls == []


def test_seeded_run_continues_generation_in_a_fresh_session_from_the_draft(tmp_path):
    p, parent, child = problem(tmp_path), tmp_path / "parent", tmp_path / "child"
    assert Experiment(Generator([(False, "Parent draft")]), Reviewer(["REVISE"]), LoopConfig(1)).run(
        p, parent).status == "budget_exhausted"

    class DraftReader(Generator):
        def generate(self, workspace, attempt, prompt, session_id, search):
            self.drafts = [*getattr(self, "drafts", []), (workspace / "results/blueprint.md").read_text()]
            return super().generate(workspace, attempt, prompt, session_id, search)

    g, v = DraftReader([(True, "Revised draft")]), Reviewer(["PASS", "REVISE"])
    result = Experiment(g, v, LoopConfig(2)).run(p, child, seed_from=parent)
    assert result.status == "budget_exhausted" and result.rounds_completed == 2
    assert [r.candidate_solution for r in v.calls] == ["Parent draft", "Revised draft"]
    assert v.calls[0].mode == "partial"  # the parent's readiness flag is kept
    assert g.drafts == ["Parent draft"] and g.calls[0]["session_id"] is None
    assert "took from an earlier run" in g.calls[0]["prompt"]
    assert "verification_inconclusive" not in g.calls[0]["prompt"] and '"PASS"' in g.calls[0]["prompt"]
    assert result.generation_turns_completed == 1 and result.candidates_generated == 1


def test_seed_must_match_the_problem_and_cannot_change_later(tmp_path):
    p, parent, child = problem(tmp_path), tmp_path / "parent", tmp_path / "child"
    Experiment(Generator([(True, "Draft")]), Reviewer(["REVISE"]), LoopConfig(1)).run(p, parent)
    other = tmp_path / "other.md"
    other.write_text("A different question.\n")
    with pytest.raises(ValueError, match="different problem"):
        Experiment(Generator([]), Reviewer([]), LoopConfig(1)).run(other, child, seed_from=parent)
    with pytest.raises(ValueError, match="another existing"):
        Experiment(Generator([]), Reviewer([]), LoopConfig(1)).run(p, child, seed_from=child)
    with pytest.raises(ValueError, match="requires --seed-from"):
        Experiment(Generator([]), Reviewer([]), LoopConfig(1)).run(p, child, seed_branch=1)
    # A run started without a seed cannot acquire one.
    with pytest.raises(FrozenRunMismatch, match="seed cannot be added"):
        Experiment(Generator([]), Reviewer([]), LoopConfig(1)).run(p, parent, seed_from=child)
    Experiment(Generator([]), Reviewer(["REVISE"]), LoopConfig(1)).run(p, child, seed_from=parent)
    (child / "inputs/seed/generation.json").write_text("{}")
    with pytest.raises(FrozenRunMismatch, match="seed snapshot changed"):
        Experiment(Generator([]), Reviewer([]), LoopConfig(1)).run(p, child)


def test_seed_from_a_wide_run_names_its_branch(tmp_path):
    p, parent = problem(tmp_path), tmp_path / "parent"
    drafts = Generator([(True, "Branch one"), (True, "Branch two")])
    Experiment(drafts, Reviewer(["REVISE", "REVISE"]), LoopConfig(1, generation_branches=2)).run(p, parent)
    with pytest.raises(ValueError, match="choose one with --seed-branch"):
        Experiment(Generator([]), Reviewer([]), LoopConfig(1)).run(p, tmp_path / "a", seed_from=parent)
    v = Reviewer(["REVISE", "REVISE"])
    run = tmp_path / "b"
    Experiment(Generator([]), v, LoopConfig(1, generation_branches=2)).run(p, run, seed_from=parent, seed_branch=2)
    candidate = read_json(parent / "branches/0002/rounds/0001/generation.json")["output"]["candidate_solution"]
    assert [r.candidate_solution for r in v.calls] == [candidate, candidate]  # every new branch
    assert read_json(run / "manifest.json")["provenance"]["seed"]["generation_artifact"] == "branches/0002/rounds/0001/generation.json"


def test_unrecorded_reference_files_are_refused_but_finder_metadata_is_not(tmp_path):
    p, run, refs = problem(tmp_path), tmp_path / "run", references(tmp_path)
    (refs / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1")
    v = Reviewer(["REVISE"] * 3)  # the double has no cache: resume replays round 1 through it
    Experiment(Generator([(True, PROOF)]), v, LoopConfig(1)).run(p, run, refs)
    assert set(read_json(run / "manifest.json")["provenance"]["references"]) == {"smith1962.md"}
    assert not (run / "inputs/references/.DS_Store").exists()
    (run / "inputs/references/.DS_Store").write_bytes(b"\x00")
    (run / "inputs/references/._smith1962.md").write_bytes(b"\x00\x05\x16\x07")
    result = Experiment(Generator([(True, PROOF)]), v, LoopConfig(2)).run(p, run, refs)
    assert result.status == "budget_exhausted" and result.rounds_completed == 2
    (run / "inputs/references/unrecorded.md").write_text("A source no manifest lists.\n")
    with pytest.raises(FrozenRunMismatch, match="frozen reference snapshot changed"):
        Experiment(Generator([]), Reviewer([]), LoopConfig(3)).run(p, run, refs)


def test_seed_refuses_other_directories_and_survives_an_interrupted_start(tmp_path):
    p, parent, child = problem(tmp_path), tmp_path / "parent", tmp_path / "child"
    review = tmp_path / "review"
    review.mkdir()
    (review / "manifest.json").write_text(json.dumps({"fingerprint": "x", "provenance": {"request": {}}}))
    with pytest.raises(ValueError, match="another existing Rethlas-OSV run"):
        Experiment(Generator([]), Reviewer([]), LoopConfig(1)).run(p, child, seed_from=review)
    Experiment(Generator([(True, "Draft")]), Reviewer(["REVISE"]), LoopConfig(1)).run(p, parent)
    # An earlier start copied the seed but stopped before writing the manifest.
    (child / "inputs/seed").mkdir(parents=True)
    (child / "inputs/seed/generation.json").write_text("partial")
    v = Reviewer(["REVISE"])
    result = Experiment(Generator([]), v, LoopConfig(1)).run(p, child, seed_from=parent)
    assert result.status == "budget_exhausted" and [r.candidate_solution for r in v.calls] == ["Draft"]
