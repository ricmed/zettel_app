"""Safe Markdown rendering for content shown in the web interface."""

from __future__ import annotations

import bleach
from markdown_it import MarkdownIt


_MARKDOWN = MarkdownIt("commonmark", {"html": False, "linkify": True}).enable(
    ["table", "linkify"]
)
_ALLOWED_TAGS = set(bleach.sanitizer.ALLOWED_TAGS) | {
    "h1", "h2", "h3", "h4", "h5", "h6",
    "p", "pre", "code", "blockquote",
    "ul", "ol", "li", "table", "thead", "tbody", "tr", "th", "td",
    "hr", "br",
}
_ALLOWED_ATTRIBUTES = {
    "a": ["href", "title"],
    "code": ["class"],
    "th": ["align"],
    "td": ["align"],
}
_ALLOWED_PROTOCOLS = {"http", "https", "mailto"}


def render_markdown(text: str | None) -> str:
    """Render Markdown while stripping unsafe HTML and URL schemes.

    Raw HTML is disabled in the Markdown parser, and the second sanitization
    pass protects the rendered output if the parser or its configuration
    changes later.
    """

    rendered = _MARKDOWN.render(text or "")
    return bleach.clean(
        rendered,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        protocols=_ALLOWED_PROTOCOLS,
        strip=True,
    )