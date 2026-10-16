"""Regression cases against the installed OSVerify dependency used by this app."""
from __future__ import annotations

import io
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from osverifier_reports import references
from osverifier_reports.reference_tools import LiteratureTools
from PIL import Image


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


@pytest.mark.parametrize('failure', ['text_missing', 'text_timeout', 'render_timeout'])
def test_installed_core_preserves_independent_pdf_representations(tmp_path, monkeypatch, failure):
    url = 'https://example.org/paper.pdf'

    class Response(io.BytesIO):
        def __init__(self):
            super().__init__(b'%PDF-fixture')
            self.url = url
            self.status = 200
            self.headers = {'Content-Type': 'application/pdf'}

    class Opener:
        def open(self, *args, **kwargs):
            return Response()

    def poppler(command, **kwargs):
        if command[0] == 'pdftotext':
            if failure == 'text_missing':
                raise FileNotFoundError('pdftotext')
            if failure == 'text_timeout':
                raise subprocess.TimeoutExpired(command, 60)
            Path(command[-1]).write_text('Original theorem: α ≤ β\f', encoding='utf-8')
        else:
            if failure == 'render_timeout':
                raise subprocess.TimeoutExpired(command, 60)
            Image.new('RGB', (20, 20), 'white').save(command[-1] + '.png')
        return SimpleNamespace(returncode=0, stderr=b'')

    monkeypatch.setattr(references, 'public_url', lambda value: value)
    monkeypatch.setattr(references.urllib.request, 'build_opener', lambda *args: Opener())
    monkeypatch.setattr(references.subprocess, 'run', poppler)
    service = LiteratureTools(references.ReferenceStore(tmp_path))
    result = service.read_reference(url, page=1, query='' if failure == 'render_timeout' else 'unextractable')
    assert result['status'] == 'passage' and result['processing_errors']
    if failure == 'render_timeout':
        assert 'α ≤ β' in result['text']
        assert result['page_image_status'] == 'unavailable'
        assert result['processing_errors'][0]['stage'] == 'page_render'
    else:
        assert result['page_image_status'] == 'available' and result['page_images']
        assert result['text_match_found'] is False
        assert result['processing_errors'][0]['stage'] == 'text_extraction'
