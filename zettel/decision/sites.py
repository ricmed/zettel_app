"""Pure builders: the inputs of one decision -> ``Decision(state, questions)``.

Each builder follows the decision model's own guidance (docs.typesafe.ai):

* **minimal state** -- only what the question needs, as a labelled JSON object;
  accuracy drops when the state carries material the question does not use;
* **one dimension per question**, composed in code rather than in a prompt;
* a **"none" option** on every ``choice`` whose list may not fit the input;
* ``score`` levels described by **concrete situations**, ordered worst to best.

Instructions and criteria exist in English and Portuguese
(``decision.instructions_language``); the content -- passages, notes, articles --
is always the original PT-BR. The questions live here, not in ``prompts/``,
because their option keys are contracts with code (``DedupeDecision``, taxonomy
category names), not prose a user tunes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Lang = Literal["en", "pt"]

NONE_OPTION = "none"


@dataclass(frozen=True)
class Decision:
    state: dict[str, Any]
    questions: dict[str, dict[str, Any]]


def _t(lang: Lang, en: str, pt: str) -> str:
    return en if lang == "en" else pt


# ── Dedupe within one source ─────────────────────────────────────────────

# `refine_existing` and `merge` have the same effect in the pipeline (the new
# note is born linked to the target), so the question only separates the three
# outcomes that differ.
DEDUPE_OPTIONS = ("create_new", "ignore", "link")


def dedupe_baseline_decision(decision: str) -> str:
    """Map the LLM's four-way ``DedupeDecision`` onto the three distinct outcomes."""
    return "link" if decision in ("refine_existing", "merge") else decision


def dedupe(*, thesis: str, definition: str, existing: list[dict[str, str]], lang: Lang) -> Decision:
    """``existing``: ``[{id, title, text}]`` -- exactly the same-source notes the LLM saw."""
    state = {
        "candidate": {"thesis": thesis, "definition": definition},
        "existing_notes": existing,
    }
    decision = {
        "type": "choice",
        "instructions": _t(
            lang,
            "Every existing note comes from the same work as the candidate. Within this work, "
            "is the candidate a new idea, a repetition, or a development of an existing note?",
            "Todas as notas existentes vem da mesma obra do candidato. Dentro desta obra, o "
            "candidato e uma ideia nova, uma repeticao ou um desenvolvimento de uma nota "
            "existente?",
        ),
        "criteria": {
            "create_new": {
                "what": _t(
                    lang,
                    "The candidate states an idea that no existing note records.",
                    "O candidato afirma uma ideia que nenhuma nota existente registra.",
                ),
                "not_for": _t(
                    lang,
                    "A restatement, or a new nuance, of an idea an existing note records.",
                    "Uma reformulacao, ou nuance nova, de ideia que uma nota existente registra.",
                ),
            },
            "ignore": {
                "what": _t(
                    lang,
                    "The candidate repeats an existing note: the same idea, nothing added.",
                    "O candidato repete uma nota existente: a mesma ideia, nada acrescentado.",
                ),
                "not_for": _t(
                    lang,
                    "A candidate that adds an aspect, a condition or depth to the existing idea.",
                    "Candidato que acrescenta aspecto, condicao ou profundidade a ideia existente.",
                ),
            },
            "link": {
                "what": _t(
                    lang,
                    "The candidate develops an idea an existing note records: a new nuance, a "
                    "fuller formulation, or the author deepening it later in the work.",
                    "O candidato desenvolve uma ideia que uma nota existente registra: nuance "
                    "nova, formulacao mais completa, ou o autor aprofundando-a adiante na obra.",
                ),
                "not_for": _t(
                    lang,
                    "An idea absent from every existing note, or a plain repetition.",
                    "Uma ideia ausente de todas as notas existentes, ou uma repeticao pura.",
                ),
            },
        },
    }
    target_criteria: dict[str, Any] = {
        note["id"]: f"{note['title']}: {note['text']}" for note in existing
    }
    target_criteria[NONE_OPTION] = _t(
        lang,
        "No existing note is about the same idea as the candidate.",
        "Nenhuma nota existente trata da mesma ideia do candidato.",
    )
    target = {
        "type": "choice",
        "instructions": _t(
            lang,
            "Which existing note is about the same idea as the candidate?",
            "Qual nota existente trata da mesma ideia do candidato?",
        ),
        "criteria": target_criteria,
    }
    return Decision(state=state, questions={"decision": decision, "target": target})


# ── Category of a garden cluster ─────────────────────────────────────────


def moc_category(
    *,
    notes: list[dict[str, str]],
    terms: list[str],
    categories: list[tuple[str, str, list[str]]],
    lang: Lang,
) -> Decision:
    """``categories``: ``[(pillar, category, leaf_topics)]`` from the taxonomy YAML."""
    criteria: dict[str, Any] = {
        name: f"{pillar}: {name}" + (f" ({', '.join(topics)})" if topics else "")
        for pillar, name, topics in categories
    }
    criteria[NONE_OPTION] = _t(
        lang,
        "None of the categories fits what these notes have in common.",
        "Nenhuma das categorias cabe no que estas notas tem em comum.",
    )
    question = {
        "type": "choice",
        "instructions": _t(
            lang,
            "Which knowledge category best describes what this group of notes has in common?",
            "Qual categoria de conhecimento melhor descreve o que este grupo de notas tem em "
            "comum?",
        ),
        "criteria": criteria,
    }
    return Decision(
        state={"notes": notes, "frequent_terms": terms}, questions={"category": question}
    )


# ── Article judge, one dimension per request ─────────────────────────────

JUDGE_DIMENSIONS = ("fidelity", "coverage", "references", "naturalness")

# Patterns the article prompt already penalises (prompts/article_judge.md).
_NATURALNESS_PATTERNS = {
    "en": (
        "em dashes; 'it is not X. It is Y.' or 'not only X, but also Y'; forced groups of "
        "three and overly symmetric lists; consecutive paragraphs of the same length or "
        "opening; rhetorical questions as transitions; sections that close by summarising "
        "themselves or 'In conclusion'; cliches (tapestry, game changer, crucial, shed light, "
        "in a world where)"
    ),
    "pt": (
        "travessao; 'nao e X. E Y.' ou 'nao apenas X, mas tambem Y'; grupos de tres forcados "
        "e listas simetricas demais; paragrafos seguidos com o mesmo tamanho ou a mesma "
        "abertura; pergunta retorica como transicao; secao que fecha resumindo a si mesma ou "
        "'Em conclusao'; cliches (tapecaria, divisor de aguas, crucial, lancar luz, em um "
        "mundo onde)"
    ),
}

_JUDGE_LEVELS: dict[str, dict[Lang, list[str]]] = {
    "fidelity": {
        "en": [
            "Several claims contradict the notes or are invented: no note supports them.",
            "Some central claims have no support in the notes.",
            "Most claims are supported; a few secondary claims go beyond the notes.",
            "Nearly every claim is supported by the notes; at most one minor extrapolation.",
            "Every claim is supported by the notes; nothing is invented.",
        ],
        "pt": [
            "Varias afirmacoes contradizem as notas ou sao inventadas: nenhuma nota as sustenta.",
            "Algumas afirmacoes centrais nao tem apoio nas notas.",
            "A maioria das afirmacoes tem apoio; algumas secundarias vao alem das notas.",
            "Quase toda afirmacao tem apoio nas notas; no maximo uma extrapolacao menor.",
            "Toda afirmacao tem apoio nas notas; nada e inventado.",
        ],
    },
    "coverage": {
        "en": [
            "The article misses the topic or covers only a marginal aspect of it.",
            "It covers the topic superficially and leaves out most of what the notes offer.",
            "It covers the main points but leaves out important notes on the topic.",
            "It covers the topic well; only minor notes are left out.",
            "It covers the topic fully, using the relevant notes.",
        ],
        "pt": [
            "O artigo erra o tema ou cobre so um aspecto marginal dele.",
            "Cobre o tema superficialmente e deixa de fora a maior parte do que as notas oferecem.",
            "Cobre os pontos principais, mas deixa de fora notas importantes sobre o tema.",
            "Cobre bem o tema; so notas menores ficam de fora.",
            "Cobre o tema por inteiro, usando as notas relevantes.",
        ],
    },
    "references": {
        "en": [
            "No source is cited or mentioned where claims need one.",
            "Sources appear rarely, in a form that does not suit the style.",
            "Sources appear in places; several claims that need one lack it, or the form varies.",
            "Sources appear where needed, in the right form, with occasional lapses.",
            "Every claim that needs a source has one, in the form the style asks for.",
        ],
        "pt": [
            "Nenhuma fonte e citada ou mencionada onde as afirmacoes precisam.",
            "Fontes aparecem raramente, numa forma que nao serve ao estilo.",
            (
                "Fontes aparecem em alguns pontos; varias afirmacoes que precisam ficam sem, "
                "ou a forma varia."
            ),
            "Fontes aparecem onde precisam, na forma certa, com lapsos ocasionais.",
            "Toda afirmacao que precisa de fonte tem uma, na forma que o estilo pede.",
        ],
    },
    "naturalness": {
        "en": [
            "Reads as machine-written: the listed patterns recur throughout.",
            "Several of the listed patterns recur in most paragraphs.",
            "The listed patterns appear in some paragraphs.",
            "One or two isolated occurrences of the listed patterns.",
            "Reads as written by a person: none of the listed patterns.",
        ],
        "pt": [
            "Soa escrito por maquina: os padroes listados se repetem do inicio ao fim.",
            "Varios dos padroes listados se repetem na maioria dos paragrafos.",
            "Os padroes listados aparecem em alguns paragrafos.",
            "Uma ou duas ocorrencias isoladas dos padroes listados.",
            "Soa escrito por uma pessoa: nenhum dos padroes listados.",
        ],
    },
}

JUDGE_TOP_LEVEL = 4  # five levels, 0..4; the LLM judge scores 0..10


def judge_dimension(
    dimension: str, *, topic: str, style: str, article: str, catalog: str, lang: Lang
) -> Decision:
    """One judge dimension, with only the state it reads.

    Fidelity needs the notes; naturalness needs only the prose.
    """
    states = {
        "fidelity": {"article": article, "notes": catalog},
        "coverage": {"topic": topic, "article": article, "notes": catalog},
        "references": {"style": style, "article": article},
        "naturalness": {"article": article},
    }
    instructions = {
        "fidelity": _t(
            lang,
            "Are the article's claims supported by the notes? The notes are the only allowed "
            "evidence.",
            "As afirmacoes do artigo tem apoio nas notas? As notas sao a unica evidencia "
            "permitida.",
        ),
        "coverage": _t(
            lang,
            "How well does the article cover the topic, given what the notes offer about it?",
            "Quao bem o artigo cobre o tema, dado o que as notas oferecem sobre ele?",
        ),
        "references": _t(
            lang,
            "How well does the article cite its sources for its style? 'academic' asks for "
            "author-date citations; 'blog' for light mentions of the source.",
            "Quao bem o artigo cita suas fontes para o estilo? 'academic' pede citacao "
            "autor-data; 'blog', mencao leve a fonte.",
        ),
        "naturalness": _t(
            lang,
            "How natural is the prose? Patterns to look for: " + _NATURALNESS_PATTERNS["en"] + ".",
            "Quao natural e a prosa? Padroes a procurar: " + _NATURALNESS_PATTERNS["pt"] + ".",
        ),
    }
    question = {
        "type": "score",
        "instructions": instructions[dimension],
        "criteria": _JUDGE_LEVELS[dimension][lang],
    }
    return Decision(state=states[dimension], questions={dimension: question})


def judge_score_0_10(score: float) -> float:
    """Place a 0..4 score on the LLM judge's 0..10 scale, for comparison only."""
    return round(score / JUDGE_TOP_LEVEL * 10, 2)


# ── Gold-set probes (scripts/probe_jev_gold.py) ──────────────────────────

_KEEP_INSTRUCTIONS = {
    "en": (
        "Would a demanding Zettelkasten curator keep this as a permanent note? A permanent note "
        "states one non-trivial idea, is understandable without the original text, and is "
        "useful outside the context it came from."
    ),
    "pt": (
        "Um curador exigente de Zettelkasten manteria isto como nota permanente? Uma nota "
        "permanente afirma uma ideia nao trivial, e compreensivel sem o texto original e e "
        "util fora do contexto de onde veio."
    ),
}

EXTRACT_CATEGORIES = ("accepted", "structural", "narrative", "promotional", "trivial", "fragmented")

_EXTRACT_CATEGORY_TEXT = {
    "accepted": (
        "Holds at least one non-trivial idea that can stand as an autonomous permanent note.",
        "Contem ao menos uma ideia nao trivial que se sustenta como nota permanente autonoma.",
    ),
    "structural": (
        "Table of contents, summary, references, headings.",
        "Indice, sumario, referencias, cabecalhos.",
    ),
    "narrative": (
        "Vague introduction, transition or preamble with no concept.",
        "Introducao vaga, transicao ou preambulo sem conceito.",
    ),
    "promotional": (
        "Advertising, marketing, commercial description.",
        "Propaganda, marketing, descricao comercial.",
    ),
    "trivial": (
        "Common sense, a basic definition of a peripheral term, an obvious statement.",
        "Senso comum, definicao basica de termo periferico, obviedade.",
    ),
    "fragmented": (
        (
            "Isolated code, notation without gloss, untranslated quotation, uninterpreted "
            "table, incomplete example."
        ),
        (
            "Codigo isolado, notacao sem glosa, citacao sem traducao, tabela sem "
            "interpretacao, exemplo incompleto."
        ),
    ),
}

_READER_LEVELS = {
    "en": [
        "Should not be in the collection.",
        "Little value as a permanent note: a transition, an obvious point or a fragment.",
        "There is an idea, but it is shallow, generic or dependent on its original context.",
        "A good idea, understandable on its own, with some limitation.",
        "A clear, non-trivial, autonomous and transferable central idea: I would write this note.",
    ],
    "pt": [
        "Nao deveria estar no acervo.",
        "Pouco valor como nota permanente: transicao, obviedade ou fragmento.",
        "Ha uma ideia, mas rasa, generica ou dependente do contexto de origem.",
        "Boa ideia, compreensivel sozinha, com alguma limitacao.",
        "Ideia central clara, nao trivial, autonoma e transferivel: eu escreveria esta nota.",
    ],
}


def _keep_noul(lang: Lang) -> dict[str, Any]:
    return {"type": "noul", "instructions": _KEEP_INSTRUCTIONS[lang]}


def extract_gold(*, source_title: str, passage: str, lang: Lang) -> Decision:
    category = {
        "type": "choice",
        "instructions": _t(
            lang,
            "What best describes this passage as material for permanent notes?",
            "O que melhor descreve esta passagem como material para notas permanentes?",
        ),
        "criteria": {
            key: (en if lang == "en" else pt) for key, (en, pt) in _EXTRACT_CATEGORY_TEXT.items()
        },
    }
    return Decision(
        state={"source": source_title, "passage": passage},
        questions={"keep": _keep_noul(lang), "category": category},
    )


def reader_gold(
    *, source_title: str, passage: str, notes: list[dict[str, str]], lang: Lang
) -> Decision:
    quality = {
        "type": "score",
        "instructions": _t(
            lang,
            "Judged as a reader, how much do the extracted notes deserve a place in the collection "
            "as permanent notes? Check that the passage supports them.",
            "Julgadas como leitor, quanto as notas extraidas merecem lugar no acervo como notas "
            "permanentes? Confira se a passagem as sustenta.",
        ),
        "criteria": _READER_LEVELS[lang],
    }
    return Decision(
        state={"source": source_title, "passage": passage, "extracted_notes": notes},
        questions={"keep": _keep_noul(lang), "quality": quality},
    )
