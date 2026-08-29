---
name: Markdown automatic links
description: The MarkdownIt initialization detail required for converting bare URLs into links.
---

Enable multiple MarkdownIt rules by passing one list of rule names. Do not pass
the second rule name as a second positional argument.

**Why:** MarkdownIt's second positional argument to `enable()` is the
`ignoreInvalid` flag. Passing `"table", "linkify"` therefore enables only the
table rule and silently treats `"linkify"` as a truthy flag, leaving bare URLs
as plain text.

**How to apply:** Whenever changing Markdown parser presets or rules, keep
`table` and `linkify` in the same list and verify a bare `https://` URL renders
as an anchor.