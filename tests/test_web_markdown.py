"""The web's Markdown rendering and its sanitizing second pass (#315: nh3, not bleach)."""

from __future__ import annotations

from zettel.markdown import render_markdown, render_text_with_math, sanitize_html


def test_markdown_keeps_structure_and_internal_links():
    html = render_markdown(
        "## Tese\n\n| a | b |\n|:--|--:|\n| 1 | 2 |\n\n```python\nx = 1\n```\n\n"
        "Ver [[ZTL - 01ABC - nota|a nota]] e https://example.org."
    )
    assert "<h2>Tese</h2>" in html
    assert "<table>" in html and "<th>a</th>" in html
    assert '<code class="language-python">' in html
    assert '<a href="/notes/01ABC">a nota</a>' in html
    assert '<a href="https://example.org">https://example.org</a>' in html


def test_markdown_escapes_raw_html_and_drops_script_links():
    html = render_markdown("<script>alert(1)</script>\n\n[x](javascript:alert(1)) <img onerror=x>")
    assert "<script" not in html
    assert 'href="javascript:' not in html
    assert "<img" not in html


def test_sanitizer_strips_what_the_parser_might_let_through():
    """The second pass holds even if the parser starts emitting raw HTML."""
    dirty = (
        '<p onclick="x()">texto</p><script>alert(1)</script>'
        '<a href="javascript:alert(1)">a</a><a href="vbscript:x">b</a>'
        '<a href="/notes/01" target="_blank" title="t">c</a>'
        '<img src="x" onerror="alert(1)"><iframe src="https://e.org"></iframe>'
        '<td align="right" style="color:red">d</td>'
    )
    clean = sanitize_html(dirty)
    assert "onclick" not in clean and "onerror" not in clean
    assert "<script" not in clean and "alert(1)</" not in clean
    assert "javascript:" not in clean and "vbscript:" not in clean
    assert "<img" not in clean and "<iframe" not in clean
    assert "style=" not in clean and "target=" not in clean
    assert '<a href="/notes/01" title="t">c</a>' in clean
    assert "<p>texto</p>" in clean


def test_markdown_renders_tex_as_mathml_without_eating_subscripts():
    html = render_markdown(
        "série $Z_t$ de grau $d$, com $\\nabla Z_t = Z_t - Z_{t-1}$.\n\n"
        "$$x = {-b \\pm \\sqrt{b^2-4ac} \\over 2a}$$\n\n"
        "inline \\(a_n\\) e display \\[e^{i\\pi}+1=0\\].\n\n"
        "Preço $10 e $20 ficam prosa.\n\n"
        "```\n$x_1 = y$\n```\n"
    )
    assert "<em>" not in html
    assert "<math" in html
    assert 'display="inline"' in html
    assert 'display="block"' in html
    assert "<msub>" in html
    assert "$Z_t$" not in html
    assert "$10" in html and "$20" in html
    assert "$x_1 = y$" in html
    assert "<script" not in html


def test_markdown_math_does_not_open_a_script_tag():
    html = render_markdown("$</math><script>alert(1)</script>$")
    assert "<script" not in html
    assert "</math><script" not in html


def test_plain_text_math_escapes_surrounding_markup():
    html = render_text_with_math("<b>tese</b> $Z_t$")
    assert "<b>" not in html
    assert "&lt;b&gt;tese&lt;/b&gt;" in html
    assert "<msub>" in html
    assert "<math" in html
