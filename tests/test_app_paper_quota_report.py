from types import SimpleNamespace

from osverifier.quota import QuotaExhausted
from osverifier_reports import automatic
from paper_review_fixtures import review


def fake_compile(tex):
    path = tex.with_suffix('.pdf')
    path.write_bytes(b'%PDF-fixture')
    return path






def test_report_writer_quota_partial_download_and_resume_state(served, monkeypatch):
    app, client = served
    run = app.runs_root/'quota-paper'
    review(run)
    monkeypatch.setattr(automatic, 'compile_pdf', fake_compile)
    monkeypatch.setattr(automatic, 'report_writer', lambda args: None)
    calls = []

    def exhausted(*a):
        calls.append(1)
        raise QuotaExhausted('fixture')

    monkeypatch.setattr(automatic, 'publish', exhausted)
    automatic.after_verify(SimpleNamespace(run_dir=run,model='test',codex_bin='test',codex_home=None))
    assert len(calls) == 1
    code, detail = client.call('GET','/api/runs/quota-paper')
    assert code == 200 and detail['status'] == 'quota_exhausted' and detail['resumable']
    assert detail['template_report']['partial']
    import urllib.request
    with urllib.request.urlopen(client.base+'/api/runs/quota-paper/template-pdf') as response:
        assert response.read() == b'%PDF-fixture'


