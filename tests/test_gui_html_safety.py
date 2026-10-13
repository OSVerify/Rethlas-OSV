"""The GUI page must never turn paper text or model output into live markup.

Reports, proofs and paper-derived questions are untrusted. These checks pin the page's
defences: a restrictive CSP, integrity-checked libraries, escaping that also covers
single quotes, no inline handlers built from run data, and Markdown that passes the
escaping renderer and an allowlist before reaching the DOM.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parents[1] / "src/rethlas_osv_gui/static/index.html"


def page() -> str:
    return PAGE.read_text(encoding="utf-8")


def inline_script() -> str:
    return re.findall(r"<script>(.*?)</script>", page(), re.S)[-1]


def test_csp_restricts_requests_and_precedes_every_resource():
    html = page()
    match = re.search(r'<meta http-equiv="Content-Security-Policy" content="([^"]+)">', html)
    assert match, "the page needs a Content-Security-Policy"
    policy = {d.split()[0]: d.split()[1:] for d in match.group(1).split(";") if d.strip()}
    assert policy["connect-src"] == ["'self'"]
    assert policy["img-src"] == ["'self'", "data:"]
    assert policy["object-src"] == ["'none'"]
    assert policy["base-uri"] == ["'none'"] and policy["form-action"] == ["'none'"]
    first_resource = min(html.index("<script"), html.index("<link"))
    assert match.start() < first_resource


def test_external_libraries_are_integrity_pinned():
    tags = re.findall(r"<(?:script|link)\b[^>]*https://[^>]*>", page())
    assert tags
    for tag in tags:
        assert re.search(r'integrity="sha384-[A-Za-z0-9+/=]{64}"', tag), tag
        assert 'crossorigin="anonymous"' in tag, tag


def test_run_data_never_becomes_inline_code_or_raw_markup():
    script = inline_script()
    assert not re.search(r'on[a-z]+="[^"]*\$\{', script), "inline handler built from data"
    assert "\"'\": \"&#39;\"" in script, "esc() must escape single quotes"
    assert 'option value="${esc(m)}"' in script
    render = script[script.index("function renderMarkdown"):]
    render = render[: render.index("\n}\n")]
    assert "sanitizeInto(el, html)" in render and "innerHTML = html" not in render


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_markdown_hooks_escape_html_and_drop_script_urls(tmp_path):
    script = inline_script()
    esc = re.search(r"^function esc\(s\).*$", script, re.M).group(0)
    block = script[
        script.index("// ---- safe Markdown rendering: begin") : script.index(
            "// ---- safe Markdown rendering: end"
        )
    ]
    probe = tmp_path / "probe.js"
    probe.write_text(
        esc
        + "\n"
        + block
        + r"""
const r = SAFE_MARKED.renderer, tag = SAFE_MARKED.tokenizer.tag;
const rules = { rules: { inline: { tag: /^<[a-z][^>]*>/i } } };
console.log(JSON.stringify({
  inline: r.html('<img src=x onerror=alert(1)>', false),
  block: r.html('<div onclick=x>t</div>', true),
  token: r.html({ text: '<b>t</b>', block: false }),
  js: r.link('javascript:alert(1)', null, 'T'),
  tab: r.link('java\tscript:alert(1)', null, 'T'),
  data: r.link('data:text/html,x', null, 'T'),
  web: r.link('https://a.example/?x=1&y=2', null, 'T'),
  relative: r.link('#sec', null, 'T'),
  image: r.image('https://tracker.example/p.png', null, 'a<b'),
  tag: tag.call(rules, '<pre>rest'),
  title: tag.call(rules, '<img src=x onerror="fetch(1)">'),
}));
"""
    )
    out = json.loads(subprocess.run(["node", str(probe)], capture_output=True, text=True, check=True).stdout)
    assert out["inline"] == "&lt;img src=x onerror=alert(1)&gt;"
    assert out["block"] == "<p>&lt;div onclick=x&gt;t&lt;/div&gt;</p>"
    assert out["token"] == "&lt;b&gt;t&lt;/b&gt;"
    assert out["js"] == out["tab"] == out["data"] == "T"
    assert out["web"] == '<a href="https://a.example/?x=1&amp;y=2">T</a>'
    assert out["relative"] == '<a href="#sec">T</a>'
    assert out["image"] == "a&lt;b"
    assert out["tag"] == {"type": "text", "raw": "<pre>", "text": "&lt;pre&gt;"}
    assert "<" not in out["title"]["text"]
