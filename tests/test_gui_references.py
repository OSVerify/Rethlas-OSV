"""GUI reference files and restarts: uploads reach the run folder, and a restart seeds a new run.

The real CLI runs with the scripted fake ``codex`` from conftest; earlier runs are built in
process with scripted backends. These tests establish plumbing only, never review quality.
"""

from __future__ import annotations

import base64
import hashlib

import pytest
from conftest import fake_codex, wait_for
from osverifier import cli as verifier_cli
from osverifier.storage import read_json
from test_inconclusive_review import PROOF, UnlocatableSource, experiment
from test_rethlas import Generator, Reviewer, problem

from rethlas_osv import cli as rethlas_cli
from rethlas_osv.runner import Experiment, LoopConfig
from rethlas_osv_gui import references as reference_files
from rethlas_osv_gui.jobs import read_job
from rethlas_osv_gui.replay import ReplayError, rebuild_command
from rethlas_osv_gui.settings import prove_argv, validate_settings, with_max_rounds

SOURCE = b"Smith (1962), On sequences.\nLemma 2. Every monotone bounded sequence converges.\n"
SCAN = b"\x89PNG\r\n\x1a\n" + bytes(range(256))  # an image: not UTF-8 text


def upload(name, data):
    return {"name": name, "data": base64.b64encode(data).decode()}


def tree_hashes(directory):
    return {
        str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(directory.rglob("*"))
        if p.is_file() and p.name != ".lock"
    }


def stop(client, run_dir):
    client.call("POST", f"/api/runs/{run_dir.name}/cancel")
    return wait_for(lambda: (j := read_job(run_dir)) and j["status"] == "exited" and j)


def stuck_run(tmp_path, run_dir):
    """A finished run whose only review ended inconclusive, with one frozen reference."""
    refs = tmp_path / "old-refs"
    refs.mkdir()
    (refs / "old.md").write_text("An earlier note.\n")
    result = experiment(UnlocatableSource("unresolved"), Generator([(True, PROOF)]), 1).run(
        problem(tmp_path), run_dir, refs
    )
    assert result.status == "budget_exhausted", result.error
    return run_dir


def test_prove_job_freezes_uploaded_reference_files_and_resumes_with_them(served, tmp_path):
    app, client = served
    client.call("PUT", "/api/settings", {"codex_bin": str(fake_codex(tmp_path))})
    status, run = client.call(
        "POST",
        "/api/runs",
        {
            "kind": "prove",
            "name": "cited",
            "problem": "Show that every monotone bounded sequence converges.",
            "references": [upload("smith1962.md", SOURCE), upload("Smith scan.png", SCAN)],
        },
    )
    assert status == 201, run
    run_dir = app.runs_root / "cited"
    stored = run_dir / "gui-inputs/references"
    assert (stored / "smith1962.md").read_bytes() == SOURCE
    assert (stored / "Smith scan.png").read_bytes() == SCAN
    argv = read_job(run_dir)["argv"]
    assert rethlas_cli.parser().parse_args(argv[3:]).references == stored

    # The runner freezes its own snapshot of the folder; the fake Codex then blocks.
    wait_for(lambda: (run_dir / "manifest.json").exists() and (run_dir / "summary.json").exists())
    frozen = read_json(run_dir / "manifest.json")["provenance"]["references"]
    assert frozen == {
        "smith1962.md": hashlib.sha256(SOURCE).hexdigest(),
        "Smith scan.png": hashlib.sha256(SCAN).hexdigest(),
    }
    assert (run_dir / "inputs/references/Smith scan.png").read_bytes() == SCAN
    detail = client.call("GET", "/api/runs/cited")[1]
    assert detail["references"] == ["Smith scan.png", "smith1962.md"] and detail["seed"] is None
    assert detail["inconclusive_reviews"] == []

    # Resume replays --references, which points inside this run.
    assert stop(client, run_dir)["exit_code"] == 130
    status, body = client.call("POST", "/api/runs/cited/resume", {})
    assert status == 200, body
    assert read_job(run_dir)["argv"] == argv
    stop(client, run_dir)


@pytest.mark.parametrize(
    "entries, message",
    [
        ([upload("../escape.md", SOURCE)], "unsupported reference file name"),
        ([upload("sub/a.md", SOURCE)], "unsupported reference file name"),
        ([upload(".DS_Store", SOURCE)], "unsupported reference file name"),
        ([upload("a.md", SOURCE), upload("A.md", SCAN)], "two reference files"),
        ([upload("empty.md", b"")], "is empty"),
        ([{"name": "bad.md", "data": "not base64!"}], "not received intact"),
        ("smith1962.md", "list"),
    ],
)
def test_bad_reference_uploads_are_refused_before_a_run_folder_exists(served, entries, message):
    app, client = served
    status, body = client.call(
        "POST", "/api/runs", {"kind": "prove", "name": "x", "problem": "P", "references": entries}
    )
    assert status == 400 and message in body["error"]
    assert list(app.runs_root.iterdir()) == []


def test_restart_reviews_the_inconclusive_draft_again_with_added_references(served, tmp_path):
    app, client = served
    client.call("PUT", "/api/settings", {"codex_bin": str(fake_codex(tmp_path))})
    parent = stuck_run(tmp_path, app.runs_root / "stuck")
    status, detail = client.call("GET", "/api/runs/stuck")
    assert status == 200 and detail["status"] == "budget_exhausted"
    assert detail["inconclusive_reviews"] == [{"round": 1, "branch": 1}]
    assert detail["references"] == ["old.md"] and detail["generation_branches"] == 1
    before = tree_hashes(parent)

    status, run = client.call(
        "POST",
        "/api/runs",
        {
            "kind": "prove",
            "name": "stuck-with-refs",
            "problem": "A different problem is ignored: a restart keeps the earlier one.",
            "seed_from": "stuck",
            "keep_references": True,
            "references": [upload("smith1962.md", SOURCE)],
        },
    )
    assert status == 201, run
    child = app.runs_root / "stuck-with-refs"
    question = read_json(parent / "manifest.json")["provenance"]["question"]
    assert (child / "gui-inputs/problem.md").read_text() == question
    assert sorted(p.name for p in (child / "gui-inputs/references").iterdir()) == [
        "old.md",
        "smith1962.md",
    ]
    argv = read_job(child)["argv"]
    parsed = rethlas_cli.parser().parse_args(argv[3:])
    assert parsed.seed_from == parent.resolve() and parsed.seed_branch is None
    assert parsed.references == child / "gui-inputs/references"

    # Round 1 reviews the earlier draft without a generation call; the fake Codex blocks there.
    wait_for(lambda: (child / "inputs/seed/generation.json").exists() and (child / "trajectory.jsonl").exists())
    frozen = read_json(child / "manifest.json")["provenance"]
    assert frozen["seed"]["run_dir"] == str(parent.resolve()) and frozen["seed"]["branch"] == 1
    assert set(frozen["references"]) == {"old.md", "smith1962.md"}
    wait_for(lambda: '"seeded_from_run"' in (child / "trajectory.jsonl").read_text())
    shown = client.call("GET", "/api/runs/stuck-with-refs")[1]
    assert shown["seed"]["run_dir"] == str(parent.resolve())

    # Resume replays --seed-from (a sibling run) and --references (inside the run).
    stop(client, child)
    status, body = client.call("POST", "/api/runs/stuck-with-refs/resume", {})
    assert status == 200, body
    assert read_job(child)["argv"] == argv
    stop(client, child)
    assert tree_hashes(parent) == before  # the earlier run is only read


def test_restart_refusals_leave_no_run_folder(served, tmp_path):
    app, client = served
    client.call("PUT", "/api/settings", {"codex_bin": str(fake_codex(tmp_path))})
    stuck_run(tmp_path, app.runs_root / "stuck")
    assert verifier_cli.main(["demo", "--run-dir", str(app.runs_root / "demo")]) == 0
    Experiment(
        Generator([(True, "Branch one"), (True, "Branch two")]),
        Reviewer(["REVISE", "REVISE"]),
        LoopConfig(1, generation_branches=2),
    ).run(problem(tmp_path), app.runs_root / "wide")
    runs = sorted(p.name for p in app.runs_root.iterdir())
    refs = [upload("smith1962.md", SOURCE)]

    def restart(**fields):
        body = {"kind": "prove", "name": "again", "references": refs, **fields}
        return client.call("POST", "/api/runs", body)

    for fields, code, message in (
        ({"seed_from": "missing"}, 404, "not found"),
        ({"seed_from": "../stuck"}, 400, "invalid run name"),
        ({"seed_from": "demo"}, 400, "only a started prove run"),
        ({"seed_from": "stuck", "references": []}, 400, "add the reference files"),
        ({"seed_from": "wide"}, 400, "choose the branch"),
        ({"seed_from": "wide", "seed_branch": 3}, 400, "between 1 and 2"),
        ({"seed_from": "stuck", "keep_references": "yes"}, 400, "true or false"),
        (
            {"seed_from": "stuck", "keep_references": True,
             "references": [upload("OLD.md", b"Another text.\n")]},
            400,
            "differs from the earlier run's reference old.md",
        ),
    ):
        status, body = restart(**fields)
        assert (status, message in body["error"]) == (code, True), (fields, body)
    (app.runs_root / "stuck/inputs/references/old.md").write_text("Edited later.\n")
    status, body = restart(seed_from="stuck", keep_references=True)
    assert status == 400 and "no longer matches" in body["error"]
    assert sorted(p.name for p in app.runs_root.iterdir()) == runs

    # A run still in progress cannot be restarted from.
    status, _ = client.call("POST", "/api/runs", {"kind": "prove", "name": "busy", "problem": "P"})
    busy = app.runs_root / "busy"
    wait_for(lambda: (busy / "manifest.json").exists())
    status, body = restart(seed_from="busy")
    assert status == 409 and "in progress" in body["error"]
    stop(client, busy)

    # Choosing a branch of a wide run passes it through.
    status, run = restart(seed_from="wide", seed_branch="2")
    assert status == 201, run
    parsed = rethlas_cli.parser().parse_args(read_job(app.runs_root / "again")["argv"][3:])
    assert parsed.seed_branch == 2 and parsed.seed_from == (app.runs_root / "wide").resolve()
    stop(client, app.runs_root / "again")


def test_identical_kept_and_uploaded_references_are_merged(tmp_path):
    kept = tmp_path / "old.md"
    kept.write_bytes(SOURCE)
    files = reference_files.combine({"OLD.md": SOURCE, "new.md": SCAN}, {"old.md": kept})
    assert files == {"old.md": kept, "new.md": SCAN}
    with pytest.raises(ValueError, match="also a folder"):
        reference_files.combine({"Papers": SOURCE}, {"papers/old.md": kept})


def test_replay_accepts_only_references_inside_the_run_and_seeds_beside_it(tmp_path):
    runs = (tmp_path / "runs").resolve()
    run_dir, inputs = runs / "child", runs / "child" / "gui-inputs"
    settings = validate_settings({"codex_bin": "codex"})
    allowed = {"codex_bins": ["codex"], "codex_homes": [""]}
    argv = prove_argv(run_dir, settings, inputs / "problem.md", references=inputs / "references",
                      seed_from=runs / "parent", seed_branch=2)
    parsed = rethlas_cli.parser().parse_args(argv[3:])
    assert (parsed.references, parsed.seed_from, parsed.seed_branch) == (inputs / "references", runs / "parent", 2)
    assert rebuild_command({"kind": "prove", "argv": argv}, run_dir, **allowed) == argv
    deeper = with_max_rounds(argv, 12)
    assert rebuild_command({"kind": "prove", "argv": deeper}, run_dir, **allowed) == deeper

    def swapped(old, new):
        return [str(new) if a == str(old) else a for a in argv]

    branch = argv.index("--seed-branch") + 1

    for tampered in (
        swapped(inputs / "references", tmp_path / "elsewhere"),
        swapped(inputs / "references", "gui-inputs/references"),
        swapped(runs / "parent", tmp_path / "other-runs" / "parent"),
        swapped(runs / "parent", run_dir),
        swapped(runs / "parent", runs),
        swapped(runs / "parent", "../parent"),
        [*argv[:branch], "0", *argv[branch + 1 :]],
        argv + ["--seed-from", str(runs / "parent")],
    ):
        with pytest.raises(ReplayError):
            rebuild_command({"kind": "prove", "argv": tampered}, run_dir, **allowed)
