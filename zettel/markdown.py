"""Safe Markdown rendering for content shown in the web interface."""

from __future__ import annotations

import html
import re
from typing import TYPE_CHECKING
from urllib.parse import unquote

import nh3
from latex2mathml.converter import convert
from markdown_it import MarkdownIt
from markdown_it.rules_block.state_block import StateBlock
from markdown_it.rules_inline import StateInline

from zettel.markdown_fences import iter_fenced_spans, offset_is_fenced

if TYPE_CHECKING:
    from markdown_it.token import Token
    from markdown_it.utils import EnvType, OptionsDict

_MARKDOWN = MarkdownIt("commonmark", {"html": False, "linkify": True}).enable(["table", "linkify"])
# Vault files keep ``<!-- zettel:auto-*:start/end -->`` so the pipeline can find
# the block. The parser has html off (CSP), so those lines would otherwise show
# as prose in the browser. Inner content stays.
_MANAGED_MARKER_LINE = re.compile(
    r"^[ \t]*<!--[ \t]*zettel:[a-z0-9-]+:(?:start|end)[ \t]*-->[ \t]*(?:\n|$)",
    re.MULTILINE | re.IGNORECASE,
)
# Written on empty manual ZTLs; after the markers are gone it would show as prose.
_MANUAL_CONNECTIONS_PLACEHOLDER_LINE = re.compile(
    r"^[ \t]*_Sincronize com sync-manual para sugestoes automaticas\._[ \t]*(?:\n|$)",
    re.MULTILINE,
)
_VAULT_WIKILINK = re.compile(
    r"\[\[(?P<label>(?P<kind>ZTL|MOC)\s*-\s*(?P<note_id>[A-Za-z0-9_-]+)(?:\s*-\s*[^\]\|]+)?)"
    r"(?:\|(?P<alias>[^\]]+))?\]\]"
)
_WIKILINK_ROUTES = {"ZTL": "/notes/", "MOC": "/mocs/"}
# A content-addressed vault image (``assets._asset_relpath``). Raster formats
# only: an SVG opened directly is a document that can carry script.
ASSET_NAME_RE = re.compile(r"img-[0-9a-f]{16}\.(?:png|jpe?g|gif|webp|bmp|avif)", re.IGNORECASE)
ASSET_URL_PREFIX = "/assets/"
# ``assets.ASSETS_DIR``; repeated here so the web never imports the asset module
# (it pulls the LLM client). tests/test_web_assets.py pins the two together.
ASSETS_FOLDER = "90_Assets"
_ASSET_REF = re.compile(rf"(?:{ASSETS_FOLDER}/)?(?P<name>[^/]+)")
_WIKI_EMBED = re.compile(r"!\[\[(?P<ref>[^\]|]+?)(?:\|(?P<alt>[^\]]*))?\]\]")
_MAX_TEX_CHARS = 4000

# MathJax-style delimiters, converted to MathML on the server. A CDN script
# (mathjax.org getting-started) would need script-src and style-src
# 'unsafe-inline'; CSP forbids both (ADR-022 addendum #315).
_MATH_TAGS = {
    "annotation",
    "math",
    "menclose",
    "merror",
    "mfenced",
    "mfrac",
    "mi",
    "mlabeledtr",
    "mmultiscripts",
    "mn",
    "mo",
    "mover",
    "mpadded",
    "mphantom",
    "mprescripts",
    "mroot",
    "mrow",
    "ms",
    "mspace",
    "msqrt",
    "mstyle",
    "msub",
    "msubsup",
    "msup",
    "mtable",
    "mtd",
    "mtext",
    "mtr",
    "munder",
    "munderover",
    "none",
    "semantics",
}
_MATH_ATTRIBUTES = {
    "accent",
    "accentunder",
    "align",
    "close",
    "columnalign",
    "columnlines",
    "columnspan",
    "depth",
    "dir",
    "display",
    "displaystyle",
    "encoding",
    "equalcolumns",
    "equalrows",
    "fence",
    "form",
    "frame",
    "framespacing",
    "height",
    "largeop",
    "linethickness",
    "lquote",
    "lspace",
    "mathbackground",
    "mathcolor",
    "mathsize",
    "mathvariant",
    "maxsize",
    "minlabelspacing",
    "minsize",
    "movablelimits",
    "notation",
    "open",
    "rowalign",
    "rowlines",
    "rowspan",
    "rquote",
    "rspace",
    "scriptlevel",
    "separator",
    "separators",
    "side",
    "stretchy",
    "symmetric",
    "voffset",
    "width",
    "xmlns",
}


def asset_url(ref: str | None) -> str | None:
    """The web URL of a ``90_Assets`` image reference, or ``None`` for anything else."""
    norm = unquote(str(ref or "")).replace("\\", "/").strip().lstrip("./")
    match = _ASSET_REF.fullmatch(norm)
    if not match or not ASSET_NAME_RE.fullmatch(match.group("name")):
        return None
    return ASSET_URL_PREFIX + match.group("name")


def _image_html(src: str, alt: str) -> str:
    return f'<img src="{html.escape(src)}" alt="{html.escape(alt)}" loading="lazy">'


def _render_vault_embed(state: StateInline, silent: bool) -> bool:
    """Render an Obsidian image embed (``![[90_Assets/img-….png]]``) as an image."""
    if state.src[state.pos : state.pos + 3] != "![[":
        return False
    match = _WIKI_EMBED.match(state.src, state.pos)
    if not match:
        return False
    src = asset_url(match.group("ref"))
    if src is None:
        return False
    if silent:
        return True
    token = state.push("vault_image", "img", 0)
    token.content = _image_html(src, (match.group("alt") or "").strip())
    state.pos = match.end()
    return True


def _render_image(tokens: list[Token], idx: int, options: OptionsDict, env: EnvType) -> str:
    """A Markdown image renders only when it points at a vault asset; else its alt text."""
    token = tokens[idx]
    alt = "".join(child.content for child in token.children or [])
    src = asset_url(str(token.attrGet("src") or ""))
    return _image_html(src, alt) if src else html.escape(alt)


def _render_ztl_wikilink(state: StateInline, silent: bool) -> bool:
    """Render Obsidian permanent-note and MOC links as internal web links."""

    if state.src[state.pos : state.pos + 2] != "[[":
        return False
    if state.pos > 0 and state.src[state.pos - 1] == "!":
        return False

    match = _VAULT_WIKILINK.match(state.src, state.pos)
    if not match:
        return False
    if silent:
        return True

    link_open = state.push("link_open", "a", 1)
    link_open.attrSet("href", _WIKILINK_ROUTES[match.group("kind")] + match.group("note_id"))
    text = state.push("text", "", 0)
    text.content = (match.group("alias") or match.group("label")).strip()
    state.push("link_close", "a", -1)
    state.pos = match.end()
    return True


def _escaped(src: str, pos: int) -> bool:
    n = 0
    i = pos - 1
    while i >= 0 and src[i] == "\\":
        n += 1
        i -= 1
    return n % 2 == 1


def _index_of_unescaped(src: str, start: int, needle: str, *, stop_at_newline: bool) -> int:
    i = start
    n = len(src)
    while i < n:
        if stop_at_newline and src[i] == "\n":
            return -1
        if _escaped(src, i):
            i += 1
            continue
        if src.startswith(needle, i):
            return i
        i += 1
    return -1


def _math_at(src: str, pos: int) -> tuple[int, int, int, bool] | None:
    """If TeX math starts at ``pos``, return ``(tex_start, tex_end, close_end, display)``."""

    n = len(src)
    if pos >= n or _escaped(src, pos):
        return None
    if src.startswith("$$", pos):
        close = _index_of_unescaped(src, pos + 2, "$$", stop_at_newline=False)
        if close <= pos + 2:
            return None
        return pos + 2, close, close + 2, True
    if src[pos] == "$":
        if pos + 1 >= n or src[pos + 1].isspace() or src[pos + 1] == "$":
            return None
        close = pos + 1
        while True:
            close = _index_of_unescaped(src, close, "$", stop_at_newline=True)
            if close < 0:
                return None
            if src.startswith("$$", close):
                close += 1
                continue
            if close > pos + 1 and src[close - 1].isspace():
                close += 1
                continue
            if close + 1 < n and src[close + 1].isdigit():
                close += 1
                continue
            break
        if close <= pos + 1:
            return None
        return pos + 1, close, close + 1, False
    if src.startswith("\\[", pos):
        close = _index_of_unescaped(src, pos + 2, "\\]", stop_at_newline=False)
        if close <= pos + 2:
            return None
        return pos + 2, close, close + 2, True
    if src.startswith("\\(", pos):
        close = _index_of_unescaped(src, pos + 2, "\\)", stop_at_newline=True)
        if close <= pos + 2:
            return None
        return pos + 2, close, close + 2, False
    return None


def tex_to_mathml(tex: str, *, display: bool) -> str:
    """Convert a TeX fragment to sanitized MathML, or a code span if it cannot."""

    stripped = tex.strip()
    if not stripped or len(stripped) > _MAX_TEX_CHARS:
        return f'<code class="math">{html.escape(tex)}</code>'
    try:
        raw = convert(stripped, display="block" if display else "inline")
    except Exception:
        return f'<code class="math">{html.escape(tex)}</code>'
    cleaned = sanitize_html(raw)
    if "<math" not in cleaned:
        return f'<code class="math">{html.escape(tex)}</code>'
    return cleaned


def _math_inline(state: StateInline, silent: bool) -> bool:
    found = _math_at(state.src, state.pos)
    if found is None:
        return False
    if silent:
        return True
    tex_start, tex_end, close_end, display = found
    token = state.push("math_block" if display else "math_inline", "math", 0)
    token.content = tex_to_mathml(state.src[tex_start:tex_end], display=display)
    token.markup = state.src[state.pos : tex_start]
    state.pos = close_end
    return True


def _math_block(state: StateBlock, start_line: int, end_line: int, silent: bool) -> bool:
    if state.is_code_block(start_line):
        return False
    pos = state.bMarks[start_line] + state.tShift[start_line]
    found = _math_at(state.src, pos)
    if found is None:
        return False
    tex_start, tex_end, close_end, display = found
    if not display:
        return False
    line = start_line
    while line < end_line and state.eMarks[line] < close_end:
        line += 1
    if line >= end_line:
        return False
    if state.src[close_end : state.eMarks[line]].strip():
        return False
    if silent:
        return True
    token = state.push("math_block", "math", 0)
    token.content = tex_to_mathml(state.src[tex_start:tex_end], display=True)
    token.map = [start_line, line + 1]
    token.markup = "$$"
    state.line = line + 1
    return True


def _render_prebuilt(tokens: list[Token], idx: int, options: OptionsDict, env: EnvType) -> str:
    return tokens[idx].content


_MARKDOWN.inline.ruler.before("link", "ztl_wikilink", _render_ztl_wikilink)
_MARKDOWN.inline.ruler.before("image", "vault_embed", _render_vault_embed)
_MARKDOWN.inline.ruler.before("escape", "texmath", _math_inline)
_MARKDOWN.block.ruler.before("fence", "texmath_block", _math_block)
_MARKDOWN.renderer.rules["math_inline"] = _render_prebuilt
_MARKDOWN.renderer.rules["math_block"] = _render_prebuilt
_MARKDOWN.renderer.rules["vault_image"] = _render_prebuilt
_MARKDOWN.renderer.rules["image"] = _render_image
# What Markdown renders, and nothing else: the inline set bleach allowed plus
# block structure. nh3 (ammonia) replaces bleach, deprecated since 2023 (#315).
_ALLOWED_TAGS = {
    "a",
    "abbr",
    "acronym",
    "b",
    "blockquote",
    "br",
    "code",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "i",
    "img",
    "li",
    "ol",
    "p",
    "pre",
    "strong",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "ul",
    *_MATH_TAGS,
}
_ALLOWED_ATTRIBUTES = {
    "a": {"href", "title"},
    "code": {"class"},
    "img": {"src", "alt", "loading"},
    "th": {"align"},
    "td": {"align"},
    **{tag: set(_MATH_ATTRIBUTES) for tag in _MATH_TAGS},
}
_ALLOWED_PROTOCOLS = {"http", "https", "mailto"}
# An image survives only as a vault asset served by ``/assets`` (img-src 'self').
_SRCLESS_IMG = re.compile(r"<img(?![^>]*\ssrc=)[^>]*>")


def _filter_attribute(tag: str, attribute: str, value: str) -> str | None:
    if tag == "img" and attribute == "src":
        return value if asset_url(value.removeprefix(ASSET_URL_PREFIX)) == value else None
    return value


def sanitize_html(html: str) -> str:
    """Keep only the tags, attributes and URL schemes Markdown output needs.

    A disallowed tag is dropped and its text kept; ``script`` and ``style``
    lose their content too. ``link_rel=None`` adds no ``rel`` to links. An
    ``img`` whose ``src`` is not a ``/assets/`` vault image is removed whole.
    """
    cleaned = nh3.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        url_schemes=_ALLOWED_PROTOCOLS,
        link_rel=None,
        attribute_filter=_filter_attribute,
    )
    return _SRCLESS_IMG.sub("", cleaned)


def render_text_with_math(text: str | None) -> str:
    """Escape prose and replace TeX delimiters with MathML.

    For fields that are not Markdown (review cards: tese, definição).
    """
    if not text:
        return ""
    src = str(text)
    parts: list[str] = []
    i = 0
    n = len(src)
    while i < n:
        if src[i] == "\\" and i + 1 < n and src[i + 1] in "$\\":
            parts.append(html.escape(src[i + 1]))
            i += 2
            continue
        found = _math_at(src, i)
        if found is None:
            j = i + 1
            while j < n and src[j] not in "$\\":
                j += 1
            parts.append(html.escape(src[i:j]))
            i = j
            continue
        tex_start, tex_end, close_end, display = found
        parts.append(tex_to_mathml(src[tex_start:tex_end], display=display))
        i = close_end
    return "".join(parts)


def strip_managed_block_markers(text: str) -> str:
    """Drop ``<!-- zettel:…:start/end -->`` lines; keep the block's inner text.

    A marker inside a fenced block stays: there it is the fence's content.
    """

    def drop_outside_fence(pattern: re.Pattern[str], value: str) -> str:
        spans = iter_fenced_spans(value)

        def repl(match: re.Match[str]) -> str:
            if offset_is_fenced(match.start(), spans):
                return match.group(0)
            return ""

        return pattern.sub(repl, value)

    stripped = drop_outside_fence(_MANAGED_MARKER_LINE, text or "")
    return drop_outside_fence(_MANUAL_CONNECTIONS_PLACEHOLDER_LINE, stripped)


def render_markdown(text: str | None) -> str:
    """Render Markdown while stripping unsafe HTML and URL schemes.

    Raw HTML is disabled in the Markdown parser, and the second sanitization
    pass protects the rendered output if the parser or its configuration
    changes later. TeX (``$...$``, ``$$...$$``, ``\\(...\\)``, ``\\[...\\]``)
    is converted to MathML before emphasis so subscripts survive. Managed-block
    markers are dropped before the parse so they are not shown as escaped prose.
    """
    return sanitize_html(_MARKDOWN.render(strip_managed_block_markers(text or "")))
