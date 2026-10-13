"""Question preparation from one paper: deterministic label extraction, verbatim checks,
rendering, and the driver/server plumbing with a scripted fake Codex."""

from __future__ import annotations

import stat
import subprocess
import sys

from conftest import wait_for
from osverifier import cli as verifier_cli
from osverifier.storage import read_json
from osverifier_reports import paper_prepare as paper_tools

from rethlas_osv_gui.jobs import read_job
from rethlas_osv_gui.settings import paper_argv, validate_settings

PAPER = r"""\documentclass{article}
\newtheorem{theorem}{Theorem}[section]
\newtheorem{prop}[theorem]{Proposition}
\newtheorem{remark}[theorem]{Remark}
\title{On Even Numbers}
\begin{document}
\section{Introduction}
Our main result is Theorem~\ref{thm:main}.
\section{Results}
\begin{theorem}[Main theorem]\label{thm:main}
Every even integer $n > 2$ is a sum of two primes.
\end{theorem}
\begin{prop}\label{prop:key}
For every prime $p > 2$, $p$ is odd.
\end{prop}
\begin{remark}\label{rem:aside}
Nothing to see here.
\end{remark}
Also see \ref{thm:main} again with \label{eq:free} outside any environment.
\end{document}
"""

STATEMENT = "Every even integer $n > 2$ is a sum of two primes."










def test_driver_prepares_inputs_and_execs_the_verifier(tmp_path):
    run = tmp_path / "run"
    inputs = run / "gui-inputs"
    inputs.mkdir(parents=True)
    (inputs / "paper.tex").write_text(PAPER)
    marker = tmp_path / "marker"
    argv = [
        "--run-dir", str(run), "--paper", str(inputs / "paper.tex"), "--mode", "manual",
        "--tags", "thm:main", "--", sys.executable, "-c", f"open({str(marker)!r}, 'w').write('ran')",
    ]  # fmt: skip
    # The driver execs into the verifier command, so run it as a child process.
    completed = subprocess.run([sys.executable, "-m", "osverifier_reports.paper_prepare", *argv], timeout=60)
    assert completed.returncode == 0 and marker.read_text() == "ran"
    assert STATEMENT in (inputs / "question.md").read_text()
    assert (inputs / "solution.md").read_text() == PAPER
    record = read_json(inputs / "extraction.json")
    assert record["request"] == {"mode": "manual", "tags": ["thm:main"], "paper": "paper.tex"}
    assert record["backend"] == {"kind": "deterministic"}
    # A rerun reuses the record; changed settings are refused instead of silently rewriting.
    assert paper_tools.main(argv[:-4]) == 0
    assert paper_tools.main(argv[:-4][:-1] + ["prop:key"]) == 1
    assert (
        paper_tools.main(
            ["--run-dir", str(run), "--paper", str(inputs / "paper.tex"), "--mode", "smart"]
        )
        == 1
    )


def test_paper_argv_parses_with_both_parsers(tmp_path):
    settings = validate_settings({})
    argv = paper_argv(
        tmp_path, settings, tmp_path / "gui-inputs" / "paper.tex", "manual", ["thm:main"], "r"
    )
    assert argv[:3] == [sys.executable, "-m", "osverifier_reports.paper_prepare"]
    split = argv.index("--")
    parsed = paper_tools.parser().parse_args(argv[3:split])
    assert parsed.mode == "manual" and parsed.tags == ["thm:main"] and parsed.model == "gpt-6-astra"
    verify = argv[split + 1 :]
    assert verify[:4] == [sys.executable, "-m", "osverifier.cli", "verify"]
    assert "--review-type" in verify and "--review-kind" not in verify
    parsed = verifier_cli.parser().parse_args(verify[3:])
    assert parsed.question.name == "question.md" and parsed.solution.name == "solution.md"
    assert parsed.review_kind == "paper" and parsed.no_early_stop and not parsed.early_stop


FAKE_CODEX_EXTRACT = r"""#!/bin/sh
# Scripted stand-in. An extraction call (workspace under gui-extraction) gets a canned
# answer; verifier calls block until interrupted.
case "$1" in
  --version) echo "codex-cli 0.162.0-fake"; exit 0 ;;
  login) echo "Logged in using ChatGPT"; exit 0 ;;
  exec)
    out=""; cd=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --output-last-message) out="$2"; shift ;;
        --cd) cd="$2"; shift ;;
      esac
      shift
    done
    case "$cd" in
      *gui-extraction*)
        cat > "$out" <<'JSON'
{"title": "On Even Numbers", "setup": "Integers are as usual.", "targets": [{"label": "thm:main", "name": "Theorem 2.1", "statement": "Every even integer $n > 2$ is a sum of two primes.", "role": "main", "reason": "announced in the introduction"}], "coverage_note": ""}
JSON
        exit 0 ;;
    esac
    sleep 60; exit 1 ;;
esac
exit 1
"""


def test_server_prepares_question_with_smart_mode_then_runs_verifier(served, tmp_path):
    app, client = served
    fake = tmp_path / "bin" / "codex"
    fake.parent.mkdir()
    fake.write_text(FAKE_CODEX_EXTRACT)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    client.call("PUT", "/api/settings", {"codex_bin": str(fake)})
    status, body = client.call(
        "POST", "/api/runs", {"kind": "verify", "name": "p", "paper": PAPER, "mode": "odd"}
    )
    assert status == 400 and "mode" in body["error"]
    status, body = client.call(
        "POST", "/api/runs", {"kind": "verify", "name": "p", "paper": PAPER, "mode": "manual"}
    )
    assert status == 400 and "label" in body["error"]
    status, run = client.call(
        "POST",
        "/api/runs",
        {
            "kind": "verify",
            "name": "Even paper",
            "paper": PAPER,
            "paper_name": "even.tex",
            "mode": "smart",
            "tags": "ignored",
        },
    )
    assert status == 201, run
    run_dir = app.runs_root / "even-paper"
    assert (run_dir / "gui-inputs/paper.tex").read_text() == PAPER
    job = read_job(run_dir)
    assert "--tags" not in job["argv"] and job["argv"][job["argv"].index("--mode") + 1] == "smart"
    wait_for(lambda: (run_dir / ".verification/manifest.json").exists())
    request = read_json(run_dir / ".verification/manifest.json")["provenance"]["request"]
    assert STATEMENT in request["question"] and request["candidate_solution"] == PAPER
    assert "Integers are as usual." in request["question"]
    status, detail = client.call("GET", "/api/runs/even-paper")
    assert detail["status"] == "running" and detail["paper_mode"] == "smart"
    assert detail["extraction"]["extraction"]["targets"][0]["label"] == "thm:main"
    assert detail["extraction"]["backend"]["attempts"] == 1
    assert detail["inputs"]["question"].startswith("Establish the main results")
    client.call("POST", "/api/runs/even-paper/cancel")
    job = wait_for(lambda: (j := read_job(run_dir)) and j["status"] == "exited" and j)
    assert job["exit_code"] == 130
    # Resume replays the driver, which reuses the extraction and re-enters the verifier.
    status, body = client.call("POST", "/api/runs/even-paper/resume", {})
    assert status == 200, body
    wait_for(
        lambda: (
            "reusing the earlier question extraction" in (run_dir / "gui-stderr.log").read_text()
        )
    )
    client.call("POST", "/api/runs/even-paper/cancel")
    wait_for(lambda: read_job(run_dir)["status"] == "exited")


def test_server_manual_mode_needs_no_model_call(served, tmp_path):
    app, client = served
    fake = tmp_path / "bin" / "codex"
    fake.parent.mkdir()
    fake.write_text(FAKE_CODEX_EXTRACT)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    client.call("PUT", "/api/settings", {"codex_bin": str(fake)})
    status, run = client.call(
        "POST",
        "/api/runs",
        {
            "kind": "verify",
            "name": "man",
            "paper": PAPER,
            "mode": "manual",
            "tags": "thm:main, prop:key",
        },
    )
    assert status == 201, run
    run_dir = app.runs_root / "man"
    wait_for(lambda: (run_dir / ".verification/manifest.json").exists())
    assert not (run_dir / "gui-extraction").exists()
    record = read_json(run_dir / "gui-inputs/extraction.json")
    assert [t["label"] for t in record["extraction"]["targets"]] == ["thm:main", "prop:key"]
    client.call("POST", "/api/runs/man/cancel")
    wait_for(lambda: read_job(run_dir)["status"] == "exited")
    # A bad label fails fast with a readable message and no verifier start.
    status, run = client.call(
        "POST",
        "/api/runs",
        {"kind": "verify", "name": "bad", "paper": PAPER, "mode": "manual", "tags": "thm:nope"},
    )
    assert status == 201
    bad = app.runs_root / "bad"
    job = wait_for(lambda: (j := read_job(bad)) and j["status"] == "exited" and j)
    assert job["exit_code"] == 1 and "label not found" in (bad / "gui-stderr.log").read_text()
    status, detail = client.call("GET", "/api/runs/bad")
    assert detail["status"] == "failed" and detail["extraction"] is None
