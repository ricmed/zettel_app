"""Contract tests between the prompt templates and the code that fills them.

Deterministic and offline: no LLM, no network. What is locked here:

* every ``prompts/*.md`` has the ``<!-- zettel:user -->`` split (except the
  ``article_anti_ai.md`` fragment, injected through ``{anti_ai}``);
* the placeholders a template uses are exactly the keys its caller passes —
  the caller's ``mapping`` dict is read straight from the source with ``ast``,
  so a new key on either side fails here;
* per-call payload never leaks into the system half (it would defeat the
  provider prompt cache);
* the JSON examples inside the prompts validate against the Pydantic schemas
  the parsers use.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest
from pydantic import BaseModel

from zettel.ask import _NO_EVIDENCE
from zettel.llm import fill_template, load_prompt_parts
from zettel.schemas import (
    ArticleOutline,
    DedupeDecision,
    DedupeResult,
    LiteratureChunkOutput,
    MOCGenerationOutput,
    MOCHubGenerationOutput,
    MOCIncrementalOutput,
    PermanentNoteLLMOutput,
    RelationType,
)

ROOT = Path(__file__).resolve().parents[1]
PROMPTS_DIR = ROOT / "prompts"

# Fragment injected into the section prompts via {anti_ai}: never loaded on its own.
NO_SPLIT = {"article_anti_ai.md"}

PLACEHOLDER_RE = re.compile(r"\{([a-z_][a-z0-9_]*)\}")

# Where each template is filled: (module, function holding the `mapping` dict).
CONSUMERS: dict[str, tuple[str, str]] = {
    "literature_note.md": ("extractor.py", "_process_chunk"),
    "dedupe_decision.md": ("extractor.py", "deduplicate_candidates"),
    "permanent_note.md": ("connector.py", "_process_candidate"),
    "ptbr_guard.md": ("connector.py", "_apply_ptbr_guard"),
    "moc_generation.md": ("gardener.py", "_create_new_moc"),
    "moc_incremental.md": ("gardener.py", "_update_existing_moc"),
    "moc_hub_generation.md": ("gardener_hub.py", "_create_new_hub_moc"),
    "moc_hub_incremental.md": ("gardener_hub.py", "_update_hub_moc"),
    "bibliographic_metadata.md": ("bibliography.py", "enrich_with_llm"),
    "image_description.md": ("assets.py", "_describe_one"),
    "ask.md": ("ask.py", "run_ask"),
    "article_outline.md": ("article.py", "generate_outline"),
    "article_section_blog.md": ("article.py", "draft_sections"),
    "article_section_academic.md": ("article.py", "draft_sections"),
    "article_query_enrich.md": ("article.py", "enrich_search_queries"),
    "article_personality.md": ("article.py", "apply_personality_rewrite"),
    "article_judge.md": ("article.py", "judge_article_body"),
}

# Payload that changes on every call: keeping it in the system half would break
# the provider prompt cache (and, for the extract/connect pair, the SQLite cache
# key semantics documented in configuracao.md).
PER_CALL_PLACEHOLDERS = {
    "article_body", "chunk_text", "context", "context_notes", "evidence",
    "existing_notes", "existing_subsections", "feedback", "filename",
    "graph_context", "hub_note_section", "judge_feedback", "neighbors_list",
    "new_definition", "new_notes_list", "new_thesis", "notes_list", "question",
    "seed_json", "text", "text_sample", "thesis",
}

# JSON examples inside these prompts must validate against the parser's schema.
EXAMPLE_SCHEMAS: dict[str, type[BaseModel]] = {
    "literature_note.md": LiteratureChunkOutput,
    "permanent_note.md": PermanentNoteLLMOutput,
    "dedupe_decision.md": DedupeResult,
    "moc_generation.md": MOCGenerationOutput,
    "moc_incremental.md": MOCIncrementalOutput,
    "moc_hub_generation.md": MOCHubGenerationOutput,
    "moc_hub_incremental.md": MOCIncrementalOutput,
    "article_outline.md": ArticleOutline,
}


def prompt_files() -> list[Path]:
    return sorted(PROMPTS_DIR.glob("*.md"))


def prompt_names() -> list[str]:
    return [p.name for p in prompt_files()]


def _caller_mapping_keys(module: str, function: str) -> set[str]:
    """Read the ``mapping = {...}`` literal of one caller straight from source."""
    tree = ast.parse((ROOT / "zettel" / module).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != function:
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Assign) or len(sub.targets) != 1:
                continue
            target = sub.targets[0]
            if (
                isinstance(target, ast.Name)
                and target.id == "mapping"
                and isinstance(sub.value, ast.Dict)
            ):
                return {
                    k.value for k in sub.value.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)
                }
    raise AssertionError(f"mapping dict nao encontrado em {module}:{function}")


def _json_examples(text: str) -> list[str]:
    """```json fences that are literal examples (no unfilled placeholder)."""
    blocks = re.findall(r"```json\s*\n(.*?)```", text, re.DOTALL)
    return [b for b in blocks if not PLACEHOLDER_RE.search(b)]


def _resolve_unions(value):
    """Collapse the ``"a | b | c"`` option idiom used in the skeletons to ``"a"``.

    The prompts show enum fields as the full option set; the schema only accepts
    one member. Alternatives are checked separately by the enum tests below.
    """
    if isinstance(value, str) and " | " in value:
        return value.split(" | ")[0].strip()
    if isinstance(value, dict):
        return {k: _resolve_unions(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_unions(v) for v in value]
    return value


# ── Inventory ─────────────────────────────────────────────────────────


def test_every_prompt_is_registered():
    assert set(prompt_names()) == set(CONSUMERS) | NO_SPLIT


def test_doctor_checks_every_prompt():
    """`zettel doctor` must fail on a checkout missing any prompt (incl. hubs)."""
    source = (ROOT / "zettel" / "cli.py").read_text(encoding="utf-8")
    block = re.search(r"prompt_files = \[(.*?)\]", source, re.DOTALL)
    assert block, "lista prompt_files nao encontrada em cli.py"
    listed = set(re.findall(r'"([^"]+\.md)"', block.group(1)))
    assert listed == set(prompt_names())


@pytest.mark.parametrize("name", prompt_names())
def test_prompt_has_user_split(name: str):
    parts = load_prompt_parts(PROMPTS_DIR / name)
    if name in NO_SPLIT:
        assert not parts.has_split
        return
    assert parts.has_split, f"{name} sem marcador <!-- zettel:user -->"
    assert parts.user_template.strip(), f"{name} com payload de usuario vazio"


# ── Placeholders ↔ caller mapping ─────────────────────────────────────


@pytest.mark.parametrize("name", sorted(CONSUMERS))
def test_placeholders_match_caller_mapping(name: str):
    module, function = CONSUMERS[name]
    parts = load_prompt_parts(PROMPTS_DIR / name)
    used = set(PLACEHOLDER_RE.findall(parts.system)) | set(
        PLACEHOLDER_RE.findall(parts.user_template)
    )
    provided = _caller_mapping_keys(module, function)
    assert used <= provided, (
        f"{name} usa placeholders sem mapping em {module}:{function}: "
        f"{sorted(used - provided)}"
    )
    assert provided <= used, (
        f"{module}:{function} passa chaves que {name} nao usa: "
        f"{sorted(provided - used)}"
    )


@pytest.mark.parametrize("name", sorted(CONSUMERS))
def test_fill_template_leaves_no_placeholder(name: str):
    module, function = CONSUMERS[name]
    mapping = {k: f"<{k}>" for k in _caller_mapping_keys(module, function)}
    parts = load_prompt_parts(PROMPTS_DIR / name)
    filled = fill_template(parts.system, mapping) + fill_template(
        parts.user_template, mapping
    )
    assert not PLACEHOLDER_RE.search(filled), (
        f"{name} ficou com placeholder apos fill_template: "
        f"{PLACEHOLDER_RE.findall(filled)}"
    )


@pytest.mark.parametrize("name", prompt_names())
def test_system_half_has_no_per_call_payload(name: str):
    parts = load_prompt_parts(PROMPTS_DIR / name)
    leaked = set(PLACEHOLDER_RE.findall(parts.system)) & PER_CALL_PLACEHOLDERS
    assert not leaked, f"{name}: payload por chamada no system: {sorted(leaked)}"


def test_literature_and_permanent_payload_stays_in_user():
    lit = load_prompt_parts(PROMPTS_DIR / "literature_note.md")
    assert "{chunk_text}" not in lit.system
    assert "{chunk_text}" in lit.user_template
    ztl = load_prompt_parts(PROMPTS_DIR / "permanent_note.md")
    assert "{thesis}" not in ztl.system
    assert "{thesis}" in ztl.user_template


def test_language_and_domain_reach_extract_connect_and_images():
    for name in ("literature_note.md", "permanent_note.md"):
        text = (PROMPTS_DIR / name).read_text(encoding="utf-8")
        assert "{language}" in text and "{domain}" in text, name
    assert "{language}" in (PROMPTS_DIR / "image_description.md").read_text(
        encoding="utf-8"
    )


# ── JSON examples ↔ Pydantic schemas ──────────────────────────────────


@pytest.mark.parametrize("name", prompt_names())
def test_json_examples_are_valid_json(name: str):
    for block in _json_examples((PROMPTS_DIR / name).read_text(encoding="utf-8")):
        json.loads(block)


@pytest.mark.parametrize("name", sorted(EXAMPLE_SCHEMAS))
def test_json_examples_validate_against_schema(name: str):
    schema = EXAMPLE_SCHEMAS[name]
    blocks = _json_examples((PROMPTS_DIR / name).read_text(encoding="utf-8"))
    assert blocks, f"{name} sem exemplo JSON"
    for block in blocks:
        schema(**_resolve_unions(json.loads(block)))


def test_literature_examples_have_no_ghost_fields():
    """`total_candidates` is ignored by the schema; nested rejection fields lie."""
    text = (PROMPTS_DIR / "literature_note.md").read_text(encoding="utf-8")
    assert "total_candidates" not in text
    for block in _json_examples(text):
        for candidate in json.loads(block).get("candidates", []):
            ghosts = {"chunk_status", "rejection_reason", "rejection_category"}
            assert not ghosts & set(candidate), (
                f"candidato com campo de chunk aninhado: {sorted(ghosts & set(candidate))}"
            )


def test_permanent_note_reject_example_has_no_note_body():
    blocks = _json_examples(
        (PROMPTS_DIR / "permanent_note.md").read_text(encoding="utf-8")
    )
    rejected = [json.loads(b) for b in blocks if json.loads(b).get("status") == "rejected"]
    assert rejected, "permanent_note.md sem exemplo de rejeicao"
    for block in rejected:
        assert set(block) == {"status", "reason", "category"}


def test_permanent_note_documents_ulid_connections():
    text = (PROMPTS_DIR / "permanent_note.md").read_text(encoding="utf-8")
    assert "ULID" in text
    assert "note_id:" in text
    for relation in RelationType:
        assert relation.value in text


# ── Prompt-specific contracts ─────────────────────────────────────────


def test_ptbr_guard_asks_for_the_json_object_the_caller_sends():
    text = (PROMPTS_DIR / "ptbr_guard.md").read_text(encoding="utf-8")
    assert "JSON" in text
    blocks = _json_examples(text)
    assert blocks, "ptbr_guard.md sem exemplo JSON"
    keys = {"thesis", "definition", "intuition", "example", "limits"}
    assert any(set(json.loads(b)) == keys for b in blocks)


def test_dedupe_prompt_lists_every_decision():
    text = (PROMPTS_DIR / "dedupe_decision.md").read_text(encoding="utf-8")
    for decision in DedupeDecision:
        assert decision.value in text, f"dedupe_decision.md nao cita {decision.value}"


def test_ask_prompt_uses_the_canonical_no_evidence_sentence():
    text = (PROMPTS_DIR / "ask.md").read_text(encoding="utf-8")
    assert _NO_EVIDENCE in text
