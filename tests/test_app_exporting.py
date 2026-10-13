from __future__ import annotations

import urllib.request

from osverifier.exporting import export_report

SAMPLE = r"""# Verifier report

- Decision: **REVISE**
- Run: incomplete

Review is unfinished. Any released feedback is partial.

## Audited feedback

### issue_1 / feedback_1 (diagnosis)

The claim $x_1 < x_2$ fails when \(x_1=2\). Check 50% & A_B.

\[
\sum_{i=1}^n i = \frac{n(n+1)}{2}
\]

**Repair**: use the following:

$$
a^2+b^2=c^2
$$

Source: candidate_solution lines 3–7

Audit: `jobs/issue_1/result.json`

## Workflow coverage

Termination: audited_rejection. Full planned scope coverage: False.

| Node | Status |
| --- | --- |
| root | completed |
"""










def test_gui_exports_match_cli_and_handle_missing_report(served):
    app, client = served
    run = app.runs_root / "example"
    run.mkdir(parents=True)
    (run / "report.md").write_text(SAMPLE)
    review = run / "reviews" / "round-1"
    review.mkdir(parents=True)
    (review / "report.md").write_text(SAMPLE)
    for suffix, directory in (("", run), ("/reviews/round-1", review)):
        for format in ("latex", "markdown"):
            with urllib.request.urlopen(
                f"{client.base}/api/runs/example{suffix}/export?format={format}"
            ) as response:
                assert response.read().decode() == export_report(directory, format)[0]
                assert response.headers["Content-Disposition"].startswith("attachment;")
                assert response.headers["Cache-Control"] == "no-store"
    assert client.call("GET", "/api/runs/example/export?format=pdf")[0] == 400
    assert client.call("GET", "/api/runs/example/reviews/missing/export")[0] == 404
    assert client.call("GET", "/api/runs/example/reviews/../export")[0] == 400




def test_native_window_enables_downloads(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace

    from rethlas_osv_gui import desktop

    webview = SimpleNamespace(settings={}, create_window=lambda *a, **k: None, start=lambda: None)
    monkeypatch.setitem(sys.modules, "webview", webview)
    monkeypatch.setattr(desktop, "installation_defaults", lambda home: {})
    server = SimpleNamespace(
        server_address=("127.0.0.1", 12345),
        serve_forever=lambda: None,
        shutdown=lambda: None,
        server_close=lambda: None,
    )
    monkeypatch.setattr(desktop, "make_server", lambda *a: server)
    assert desktop.main(["--home", str(tmp_path)]) == 0
    assert webview.settings["ALLOW_DOWNLOADS"] is True




