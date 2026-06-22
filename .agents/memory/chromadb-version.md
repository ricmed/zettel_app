---
name: ChromaDB version lock
description: ChromaDB must stay at 0.5.20 — the Replit package firewall blocks 1.x.
---

## Rule
Always install/pin `chromadb==0.5.20`. Do not attempt to upgrade to 1.x.

**Why:** Replit's package proxy returns 403 for chromadb>=1.0.0 as of June 2026.

**How to apply:** If chromadb is ever uninstalled or upgraded, reinstall with `pip install chromadb==0.5.20`.
