"""Safe Markdown rendering for content shown in the web interface."""

from __future__ import annotations

import re

import nh3
from markdown_it import MarkdownIt
from markdown_it.rules_inline import StateInline

_MARKDOWN = MarkdownIt("commonmark", {"html": False, "linkify": True}).enable(["table", "linkify"])
_ZTL_WIKILINK = re.compile(
    r"\[\[(?P<label>ZTL\s*-\s*(?P<note_id>[A-Za-z0-9_-]+)(?:\s*-\s*[^\]\|]+)?)(?:\|(?P<alias>[^\]]+))?\]\]"
)


def _render_ztl_wikilink(state: StateInline, silent: bool) -> bool:
    """Render Obsidian permanent-note links as internal web links."""

    if state.src[state.pos : state.pos + 2] != "[[":
        return False
    if state.pos > 0 and state.src[state.pos - 1] == "!":
        return False

    match = _ZTL_WIKILINK.match(state.src, state.pos)
    if not match:
        return False
    if silent:
        return True

    link_open = state.push("link_open", "a", 1)
    link_open.attrSet("href", f"/notes/{match.group('note_id')}")
    text = state.push("text", "", 0)
    text.content = (match.group("alias") or match.group("label")).strip()
    state.push("link_close", "a", -1)
    state.pos = match.end()
    return True


_MARKDOWN.inline.ruler.before("link", "ztl_wikilink", _render_ztl_wikilink)
# What Markdown renders, and nothing else: the inline set bleach allowed plus
# block structure. nh3 (ammonia) replaces bleach, deprecated since 2023 (#315).
_ALLOWED_TAGS = {
    "a",
    "abbr",
    "acronym",
    "b",
    "em",
    "i",
    "strong",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "p",
    "pre",
    "code",
    "blockquote",
    "ul",
    "ol",
    "li",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
    "hr",
    "br",
}
_ALLOWED_ATTRIBUTES = {
    "a": {"href", "title"},
    "code": {"class"},
    "th": {"align"},
    "td": {"align"},
}
_ALLOWED_PROTOCOLS = {"http", "https", "mailto"}


def sanitize_html(html: str) -> str:
    """Keep only the tags, attributes and URL schemes Markdown output needs.

    A disallowed tag is dropped and its text kept; ``script`` and ``style``
    lose their content too. ``link_rel=None`` adds no ``rel`` to links.
    """
    return nh3.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        url_schemes=_ALLOWED_PROTOCOLS,
        link_rel=None,
    )


def render_markdown(text: str | None) -> str:
    """Render Markdown while stripping unsafe HTML and URL schemes.

    Raw HTML is disabled in the Markdown parser, and the second sanitization
    pass protects the rendered output if the parser or its configuration
    changes later.
    """
    return sanitize_html(_MARKDOWN.render(text or ""))
