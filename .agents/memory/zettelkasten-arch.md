---
name: Zettelkasten pipeline architecture
description: Key design decisions and non-obvious conventions for the Zettelkasten pipeline.
---

## Shared LLM module
`zettel/llm.py` contains `get_llm()`, `call_llm()`, `load_prompt()`, `extract_json()`.
All three phases (extractor, connector, gardener) must import from here — never redeclare.

**Why:** The functions were duplicated verbatim in all three files, causing drift bugs.

## Deduplication keys
When extractor decides REFINE_EXISTING or MERGE, it stores keys `refines_note_id` and `refine_reason` in the candidate dict.
connector.py reads exactly those keys. Do not use `merge_target` / `merge_reason`.

**Why:** Key mismatch was a silent bug where refinements were never applied.

## PT-BR guard
`connector._apply_ptbr_guard` must serialize the note fields as a JSON object, send to LLM, and parse the JSON response back into individual fields.
Do NOT split corrected_text on "\n" — that destroys multi-paragraph definitions.

**Why:** Old line-split approach silently truncated most of the generated note content.

## `doctor` command timeout
`python -m zettel doctor` attempts live connections (ChromaDB, OpenAI). It will hang/timeout in the Replit shell if OPENAI_API_KEY is not set. Use `python -m zettel --help` for import validation instead.
