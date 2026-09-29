---
name: Bibliographic review boundary
description: Why duplicate reuse and retry semantics matter for reviewed bibliography
---

For a confirmed bibliographic review, only report success if the confirmed fields and ABNT reference are persisted on the resulting source. Reusing an existing source with different metadata must be an explicit conflict until a deliberate source-update flow exists. Keep a failed confirmed harvest's preparation available for retry; an interrupted confirmed harvest should resume safely.

**Why:** The harvester's deduplication and incomplete-source resume paths can bypass bibliographic inference and reuse an existing SRC. Without an explicit boundary, the UI could say a review succeeded while saving the old reference. A worker restart can otherwise strand a user's confirmed changes.

**How to apply:** Any new deduplication shortcut, source-update path, or job recovery change in the selected-file web harvest must preserve this invariant. CLI and unattended pipeline behavior remain separate.