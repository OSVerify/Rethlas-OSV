from __future__ import annotations

import json

from osverifier_reports.writer import (
    Draft,
    Fidelity,
)


def make_source(path):
    path.mkdir(parents=True)
    source = {
        "request_id": "Trial",
        "decision": "REVISE",
        "run_status": "completed",
        "backend_kind": "scripted_demo",
        "feedback": [
            {
                "issue_id": "i1",
                "feedback_id": "f1",
                "kind": "diagnosis",
                "text": "The converse is missing.",
                "source_spans": [
                    {"document": "candidate_solution", "start_line": 1, "end_line": 2}
                ],
                "audit_artifact": "jobs/audit.json",
            },
            {
                "issue_id": "i1",
                "feedback_id": "f2",
                "kind": "repair_request",
                "text": "Supply the converse.",
                "source_spans": [],
                "audit_artifact": "jobs/audit.json",
            },
        ],
    }
    (path / "report.json").write_text(json.dumps(source))
    (path / "diagnostics.json").write_text("PRIVATE_SENTINEL")
    return source


def plan():
    return Draft.model_validate(
        {
            "overview": "The review asks for the missing converse.",
            "entries": [
                {
                    "issue_id": "i1",
                    "title": "Missing converse",
                    "issue_type": "Omission",
                    "feedback": [
                        {"ref": 0, "section": "Concern"},
                        {"ref": 1, "section": "Suggestion"},
                    ],
                }
            ],
        }
    )


class Fake:
    def __init__(self, approve=True):
        self.approve = approve
        self.calls = []

    def call(self, role, payload, schema, directory):
        self.calls.append(role)
        assert "PRIVATE_SENTINEL" not in json.dumps(payload)
        return plan() if schema is Draft else Fidelity(approved=self.approve, reasons=[])








def test_gui_report_job_is_separate_and_requires_stopped_input(served, monkeypatch):
    app, client = served
    run = app.runs_root / "example"
    make_source(run)
    calls = []
    monkeypatch.setattr(app.jobs, "start", lambda *args, **kwargs: calls.append((args, kwargs)))
    status, body = client.call("POST", "/api/runs/example/report-draft", {})
    assert status == 202
    output = calls[0][0][0]
    assert output.parent == app.home / "report-drafts"
    assert not output.is_relative_to(run)
    assert "osverifier_reports.writer" in calls[0][0][2]
    report = json.loads((run / "report.json").read_text())
    report["run_status"] = "running"
    (run / "report.json").write_text(json.dumps(report))
    assert client.call("POST", "/api/runs/example/report-draft", {})[0] == 400
    assert client.call("GET", f"/api/report-drafts/{body['draft_id']}/download")[0] == 404


def test_gui_draft_end_to_end_with_scripted_codex(served, tmp_path):
    import sys
    import urllib.request

    from conftest import wait_for

    app, client = served
    run = app.runs_root / "report-demo"
    make_source(run)
    binary = tmp_path / "scripted-codex"
    binary.write_text(
        f"#!{sys.executable}\n"
        + """
import json, sys
from pathlib import Path
args=sys.argv
if '--version' in args:
    print('codex-cli 0.162.0-fake'); raise SystemExit
prompt=sys.stdin.read()
if 'check-referee-report' in prompt:
    result={'approved':True,'reasons':['Matches the released feedback.']}
else:
    result={'overview':'The converse is missing.','entries':[{'issue_id':'i1','title':'Missing converse','issue_type':'Omission','feedback':[{'ref':0,'section':'Concern'},{'ref':1,'section':'Suggestion'}]}]}
Path(args[args.index('--output-last-message')+1]).write_text(json.dumps(result))
"""
    )
    binary.chmod(0o755)
    app.update_settings({"codex_bin": str(binary), "codex_home": str(tmp_path / "account")})
    status, job = client.call("POST", "/api/runs/report-demo/report-draft", {})
    assert status == 202
    endpoint = f"/api/report-drafts/{job['draft_id']}"
    result = wait_for(
        lambda: (s := client.call("GET", endpoint)[1]) and s["status"] != "running" and s
    )
    assert result["status"] == "completed", result
    with urllib.request.urlopen(client.base + endpoint + "/download") as response:
        tex = response.read().decode()
        assert "Missing converse" in tex and "Supply the converse." in tex
    assert not (run / "status.json").exists()












def test_gui_automatic_pdf_download(served, monkeypatch):
    import urllib.request

    from osverifier_reports import automatic

    app, client = served
    run = app.runs_root / "auto-paper"
    make_source(run)
    directory = automatic.report_directory(run)
    directory.mkdir(parents=True)
    (directory / "status.json").write_text('{"status":"running","phase":"compiling"}')
    assert client.call("GET", "/api/runs/auto-paper/template-pdf")[0] == 404
    (directory / "report.pdf").write_bytes(b"%PDF-test")
    (directory / "status.json").write_text('{"status":"completed"}')
    code, detail = client.call("GET", "/api/runs/auto-paper")
    assert code == 200 and detail["template_report"]["status"] == "completed"
    with urllib.request.urlopen(client.base + "/api/runs/auto-paper/template-pdf") as response:
        assert response.headers["Content-Type"] == "application/pdf"
        assert response.read() == b"%PDF-test"


def test_classifier_and_report_writer_use_explicit_run_limits(tmp_path, served, monkeypatch):
    import stat

    from osverifier.cli import parser as verify_parser
    from osverifier_reports.ocr import parser as pdf_parser
    from osverifier_reports.writer import report_writer

    from rethlas_osv_gui.settings import paper_argv, verify_argv

    codex = tmp_path / "codex"
    codex.write_text("#!/bin/sh\n[ \"$1\" = --version ] && echo codex-cli 0.162.0-fake && exit 0\nexit 1\n")
    codex.chmod(codex.stat().st_mode | stat.S_IEXEC)
    (tmp_path / "account").mkdir()
    account = ["--model", "m", "--codex-bin", str(codex), "--codex-home", str(tmp_path / "account")]

    def limits(args):
        writer = report_writer(args)
        return writer.timeout, writer.reasoning_effort

    verify = ["verify", "--request", "r.json", "--run-dir", "out", *account]
    pdf = ["verify-pdf", "--ocr-manifest", "m.json", "--run-dir", "out", *account]
    explicit = ["--timeout-seconds", "3600", "--reasoning-effort", "xhigh"]
    for parse, argv in ((verify_parser().parse_args, verify), (pdf_parser().parse_args, pdf)):
        assert limits(parse(argv)) == (180, "high")  # Editorial defaults when not set.
        assert limits(parse(argv + explicit)) == (3600, "xhigh")
        assert limits(parse(argv + ["--reasoning-effort", "medium"])) == (180, "medium")

    # GUI runs pass the chosen settings explicitly, including the paper wrapper's verify command.
    app, client = served
    settings = {**app.settings(), "codex_bin": str(codex), "codex_home": str(tmp_path / "account"),
                "verify": {**app.settings()["verify"], "timeout_seconds": 1234.0, "reasoning_effort": "low"}}
    gui = verify_argv(tmp_path / "run", settings, tmp_path / "q.md", tmp_path / "s.md", "r")
    assert limits(verify_parser().parse_args(gui[3:])) == (1234.0, "low")
    wrapped = paper_argv(tmp_path / "run", settings, tmp_path / "paper.tex", "smart", [], "r")
    assert limits(verify_parser().parse_args(wrapped[wrapped.index("--") + 4:])) == (1234.0, "low")

    # The GUI report-draft job forwards the same limits to the writer CLI.
    client.call("PUT", "/api/settings", {"codex_bin": str(codex), "verify": {"timeout_seconds": 1234, "reasoning_effort": "low"}})
    make_source(app.runs_root / "drafted")
    started = []
    monkeypatch.setattr(app.jobs, "start", lambda output, kind, argv, settings, **kw: started.append(argv))
    status, _ = client.call("POST", "/api/runs/drafted/report-draft", {})
    assert status == 202
    argv = started[0]
    assert argv[argv.index("--timeout-seconds") + 1] == "1234.0" and argv[argv.index("--reasoning-effort") + 1] == "low"
