"""Shadow hooks: what the decision model would decide, next to what the pipeline did.

Each ``shadow_*`` function is called right after the pipeline has taken its own
decision. It does nothing when the site is ``off``; otherwise it builds the
question (``sites``), asks it with order permutations (``permute``), and writes
one ``decision_shadow`` row holding the baseline and the answer. It returns
nothing and the caller never reads the row: the pipeline's output is identical
with shadow on or off.

A row is keyed by (site, subject, state checksum). Re-running an unchanged
decision refreshes the baseline and reuses the stored answer, so a repeated
``review`` or ``garden`` does not ask again.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from zettel.config import AppConfig, DecisionSite
from zettel.decision import client as decision_client
from zettel.decision import permute, sites
from zettel.hashing import extract_embeddable_text, sha256_hex
from zettel.state import StateDB

logger = logging.getLogger(__name__)

# The model reads at most 32k tokens of state plus its longest question. Leave
# headroom for the chars/4 estimate being rough on accented PT-BR.
STATE_TOKEN_LIMIT = 28_000

DEDUPE_NOTE_CHARS = 200  # same excerpt `extractor._format_existing_notes` shows the LLM
MOC_MAX_NOTES = 20
MOC_NOTE_CHARS = 300


def enabled(cfg: AppConfig, site: DecisionSite) -> bool:
    return getattr(cfg.decision.sites, site) == "shadow"


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _run(
    cfg: AppConfig,
    db: StateDB,
    site: DecisionSite,
    subject_id: str,
    decision: sites.Decision,
    baseline: dict[str, Any],
) -> dict[str, Any] | None:
    """Ask (or reuse) one decision and record it. Returns the folded answers."""
    dcfg = cfg.decision
    client = decision_client.get_decision_client(cfg)
    checksum = sha256_hex(
        _dumps(
            {
                "model": client.model,
                "permutations": dcfg.order_permutations,
                "state": decision.state,
                "questions": decision.questions,
            }
        )
    )
    record = {
        "site": site,
        "subject_id": subject_id,
        "state_checksum": checksum,
        "model": client.model,
        "state": decision.state,
        "baseline": baseline,
    }

    stored = db.get_decision_shadow(site, subject_id, checksum)
    if stored and not stored["error"]:
        db.record_decision_shadow(
            **record,
            jev=stored["jev"],
            latency_ms=stored["latency_ms"],
            input_tokens=stored["input_tokens"],
        )
        return stored["jev"]

    longest_question = max(len(_dumps(q)) for q in decision.questions.values())
    if (len(_dumps(decision.state)) + longest_question) // 4 > STATE_TOKEN_LIMIT:
        db.record_decision_shadow(
            **record, jev=None, latency_ms=None, input_tokens=None, error="skipped:too_large"
        )
        return None

    result = client.ask(
        decision.state,
        permute.expand(decision.questions, dcfg.order_permutations),
        label=site,
    )
    jev = permute.collapse(decision.questions, result.answers) if result.ok else None
    db.record_decision_shadow(
        **record,
        jev=jev,
        latency_ms=result.latency_ms,
        input_tokens=result.input_tokens,
        error=result.error,
    )
    return jev


# ── Dedupe within one source (review) ────────────────────────────────────


def shadow_dedupe(
    cfg: AppConfig,
    db: StateDB,
    *,
    concept_id: str,
    thesis: str,
    definition: str,
    same_source: list[dict[str, Any]],
    llm_decision: str,
    llm_target: str | None,
) -> None:
    """``same_source``: the Chroma hits the dedupe LLM was shown, in rank order."""
    if not enabled(cfg, "dedupe"):
        return
    existing = [
        {
            "id": note.get("id") or "",
            "title": (note.get("metadata") or {}).get("title") or "",
            "text": (note.get("document") or "")[:DEDUPE_NOTE_CHARS],
        }
        for note in same_source
    ]
    decision = sites.dedupe(
        thesis=thesis,
        definition=definition,
        existing=existing,
        lang=cfg.decision.instructions_language,
    )
    baseline = {
        "decision": sites.dedupe_baseline_decision(llm_decision),
        "llm_decision": llm_decision,
        "target": llm_target or sites.NONE_OPTION,
    }
    _run(cfg, db, "dedupe", concept_id, decision, baseline)


# The reviewer only sees what the LLM flagged as redundant (`dedupe_pending`):
# keeping it says "not a repetition", discarding it says "a repetition".
_REVIEWER_VERDICT = {"keep": "not_ignore", "discard": "ignore"}


def label_dedupe(db: StateDB, concept_id: str, action: str) -> None:
    """Attach the reviewer's m/d decision to the concept's shadow rows, if any."""
    db.set_decision_shadow_human("dedupe", concept_id, _REVIEWER_VERDICT[action])


# ── Category of a garden cluster ─────────────────────────────────────────


def categories_with_topics(cfg: AppConfig) -> list[tuple[str, str, list[str]]]:
    from zettel.gardener_assign import category_pairs
    from zettel.taxonomy import TaxonomyLoadError, load_moc_taxonomy

    topics: dict[str, list[str]] = {}
    if cfg.gardener.topics_path is not None:
        try:
            tax = load_moc_taxonomy(cfg.gardener.topics_path)
        except TaxonomyLoadError:
            tax = None
        if tax is not None:
            topics = {c.nome: c.topicos for p in tax.taxonomia_conhecimento for c in p.categorias}
    return [(pillar, name, topics.get(name, [])) for pillar, name in category_pairs(cfg.gardener)]


def shadow_moc_category(
    cfg: AppConfig,
    db: StateDB,
    *,
    category: str,
    note_ids: list[str],
    terms: list[str],
) -> None:
    """``category``: the bucket the embedding argmax put the cluster in (or ``_unassigned``)."""
    if not enabled(cfg, "moc_category"):
        return
    categories = categories_with_topics(cfg)
    if not categories:
        return
    sorted_ids = sorted(note_ids)
    notes = []
    for nid in sorted_ids[:MOC_MAX_NOTES]:
        row = db.get_note(nid)
        if row:
            notes.append(
                {
                    "title": row.get("title") or "",
                    "text": extract_embeddable_text(row.get("body") or "")[:MOC_NOTE_CHARS],
                }
            )
    if not notes:
        return
    decision = sites.moc_category(
        notes=notes,
        terms=terms,
        categories=categories,
        lang=cfg.decision.instructions_language,
    )
    subject_id = sha256_hex("|".join(sorted_ids))  # the gardener's cluster signature
    _run(cfg, db, "moc_category", subject_id, decision, {"category": category})


# ── Article judge ────────────────────────────────────────────────────────


def shadow_article_judge(
    cfg: AppConfig,
    db: StateDB,
    *,
    topic: str,
    style: str,
    catalog: str,
    body: str,
    llm_scores: dict[str, Any],
) -> None:
    """One request per dimension; the LLM's ``feedback`` and the rewrite loop are untouched."""
    if not enabled(cfg, "article_judge"):
        return
    article_key = sha256_hex(f"{topic}|{style}|{body}")[:16]
    for dimension in sites.JUDGE_DIMENSIONS:
        decision = sites.judge_dimension(
            dimension,
            topic=topic,
            style=style,
            article=body,
            catalog=catalog,
            lang=cfg.decision.instructions_language,
        )
        baseline = {
            "score_0_10": llm_scores.get(dimension),
            "average": llm_scores.get("average"),
            "verdict": llm_scores.get("verdict"),
        }
        jev = _run(cfg, db, "article_judge", f"{article_key}:{dimension}", decision, baseline)
        if jev and dimension in jev:
            logger.debug(
                "Juiz shadow %s: jev=%.2f llm=%s",
                dimension,
                sites.judge_score_0_10(float(jev[dimension]["score"])),
                baseline["score_0_10"],
            )
