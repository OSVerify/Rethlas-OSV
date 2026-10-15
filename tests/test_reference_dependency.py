"""Regression cases against the installed OSVerify dependency used by this app."""
from __future__ import annotations

import io

import pytest
from osverifier_reports import references
from osverifier_reports.reference_tools import LiteratureTools


@pytest.mark.parametrize('content_type,body,kind', [
    ('text/html', '<html><main><h1>Project</h1><p>Research notes and implementation details for the reported experiment.</p></main></html>', 'web_page'),
    ('text/html', '<html><main><div class="toc-visible:md:grid-cols-10"><h1>Results</h1><p>The benchmark score was 83 under the described evaluation procedure.</p></div></main></html>', 'web_page'),
    ('text/plain', '# Result\nThe benchmark score was 83.\n', 'text_file'),
    ('text/plain', '# Example\n```html\n<html><body>Hello</body></html>\n```\n', 'text_file'),
])
def test_installed_core_reads_web_and_literal_text(tmp_path, monkeypatch, content_type, body, kind):
    url = 'https://example.org/source'

    class Response(io.BytesIO):
        def __init__(self):
            super().__init__(body.encode())
            self.url = url
            self.status = 200
            self.headers = {'Content-Type': content_type}

    class Opener:
        def open(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(references, 'public_url', lambda value: value)
    monkeypatch.setattr(references.urllib.request, 'build_opener', lambda *args: Opener())
    service = LiteratureTools(references.ReferenceStore(tmp_path))
    result = service.resolve_reference(url)['retrieval']
    assert result['status'] == 'available' and result['source_kind'] == kind
    passage = service.read_reference(url)
    assert passage['status'] == 'passage' and passage['source_kind'] == kind
    if kind == 'text_file':
        assert passage['text'] == '\n'.join(f'{i}: {line}' for i, line in enumerate(body.splitlines(), 1))
