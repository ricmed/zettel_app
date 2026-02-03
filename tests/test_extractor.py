"""Tests for extractor candidate filtering."""

from zettel.config import AppConfig, ExtractionConfig
from zettel.extractor import _filter_candidates
from zettel.schemas import PermanentNoteCandidate


def _make_candidate(**overrides) -> PermanentNoteCandidate:
    """Helper to build a valid candidate with sensible defaults."""
    defaults = {
        "thesis": "Gradient descent converge mais rapido com learning rate adaptativo",
        "definition": (
            "O algoritmo de gradient descent ajusta os pesos do modelo iterativamente "
            "na direcao oposta ao gradiente da funcao de perda, e taxas adaptativas "
            "como Adam aceleram a convergencia em superficies nao convexas"
        ),
        "intuition": "Como descer uma montanha ajustando o tamanho do passo",
        "limits": "Pode divergir com learning rates muito altos",
        "anchor_quote": "adaptive learning rates converge faster in practice than fixed schedules",
        "source_locator": "p.42",
        "tags": ["otimizacao", "deep-learning"],
        "relevance_score": 4,
    }
    defaults.update(overrides)
    return PermanentNoteCandidate(**defaults)


def _make_config(**overrides) -> AppConfig:
    """Helper to build an AppConfig with custom extraction settings."""
    ext_kwargs = {
        "min_relevance_score": 3,
        "min_thesis_words": 5,
        "require_anchor_quote": True,
        "min_definition_words": 10,
    }
    ext_kwargs.update(overrides)
    return AppConfig(extraction=ExtractionConfig(**ext_kwargs))


def test_filter_candidates_by_relevance():
    """Candidates below relevance threshold are rejected."""
    cfg = _make_config(min_relevance_score=3)
    candidates = [
        _make_candidate(relevance_score=2),
        _make_candidate(relevance_score=3),
    ]
    approved, rejected = _filter_candidates(candidates, cfg)
    assert len(approved) == 1
    assert len(rejected) == 1
    assert rejected[0].relevance_score == 2


def test_filter_candidates_by_thesis_length():
    """Candidates with thesis shorter than min_thesis_words are rejected."""
    cfg = _make_config(min_thesis_words=5)
    candidates = [
        _make_candidate(thesis="Curta demais"),  # 2 words
        _make_candidate(thesis="Esta tese tem palavras suficientes para passar"),
    ]
    approved, rejected = _filter_candidates(candidates, cfg)
    assert len(approved) == 1
    assert len(rejected) == 1


def test_filter_candidates_by_definition_length():
    """Candidates with definition shorter than min_definition_words are rejected."""
    cfg = _make_config(min_definition_words=10)
    candidates = [
        _make_candidate(definition="Muito curta"),  # 2 words
        _make_candidate(),  # default definition has >10 words
    ]
    approved, rejected = _filter_candidates(candidates, cfg)
    assert len(approved) == 1
    assert len(rejected) == 1


def test_filter_candidates_require_anchor():
    """Candidates without anchor_quote are rejected when required."""
    cfg = _make_config(require_anchor_quote=True)
    candidates = [
        _make_candidate(anchor_quote=""),
        _make_candidate(anchor_quote="   "),  # whitespace only
        _make_candidate(),  # default has anchor_quote
    ]
    approved, rejected = _filter_candidates(candidates, cfg)
    assert len(approved) == 1
    assert len(rejected) == 2


def test_filter_candidates_anchor_not_required():
    """Candidates without anchor_quote pass when not required."""
    cfg = _make_config(require_anchor_quote=False)
    candidates = [
        _make_candidate(anchor_quote=""),
    ]
    approved, rejected = _filter_candidates(candidates, cfg)
    assert len(approved) == 1
    assert len(rejected) == 0


def test_filter_candidates_all_pass():
    """Valid candidates pass all checks."""
    cfg = _make_config()
    candidates = [
        _make_candidate(relevance_score=4),
        _make_candidate(relevance_score=5),
    ]
    approved, rejected = _filter_candidates(candidates, cfg)
    assert len(approved) == 2
    assert len(rejected) == 0
