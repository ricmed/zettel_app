"""The single client of the typed decision model (ADR-055).

Questions arrive as plain API-shaped dicts (``{"type": "choice", "instructions":
..., "criteria": ...}``) so the builders in ``sites.py`` stay pure and free of the
SDK. The SDK is imported lazily: ``zettel --help`` and every test that never
asks a question do not load it.

**Fail-open.** A missing package, a missing credential or any API error becomes
``DecisionResult(error=...)``. The layer only observes the pipeline, so it must
never be the reason a run stops.

``get_decision_client`` is the one seam: tests replace it (or the SDK builder
below it) and never reach the network.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from zettel.config import AppConfig, DecisionConfig
from zettel.credentials import DECISION_PROVIDER_ENV, has_credential

logger = logging.getLogger(__name__)

_QUESTION_CLASSES = {"noul": "Noul", "choice": "Choice", "score": "Score"}


class DecisionUnavailable(RuntimeError):
    """The decision model cannot be reached from this process (package or key)."""


@dataclass(frozen=True)
class DecisionResult:
    answers: dict[str, dict[str, Any]] = field(default_factory=dict)
    input_tokens: int = 0
    latency_ms: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def _build_sdk_client(dcfg: DecisionConfig) -> Any:
    try:
        import typesafe_sdk as sdk
    except ImportError as exc:
        raise DecisionUnavailable("pacote typesafe-sdk nao instalado") from exc
    if not has_credential(DECISION_PROVIDER_ENV[dcfg.provider]):
        raise DecisionUnavailable("TYPESAFE_API_KEY ausente")
    return sdk.TypeSafeClient(
        model=dcfg.model,
        base_url=dcfg.base_url,
        timeout=dcfg.timeout_s,
        retry=sdk.RetryPolicy(max_retries=dcfg.max_retries, timeout=dcfg.timeout_s),
    )


def _typed_questions(questions: dict[str, dict[str, Any]]) -> dict[str, Any]:
    import typesafe_sdk as sdk

    typed = {}
    for name, question in questions.items():
        cls = getattr(sdk, _QUESTION_CLASSES[question["type"]])
        typed[name] = cls(**{k: v for k, v in question.items() if k != "type"})
    return typed


class DecisionClient:
    def __init__(self, dcfg: DecisionConfig) -> None:
        self.dcfg = dcfg
        self._sdk_client: Any = None
        # Package or key missing: decided once per process, warned once.
        self._unavailable = ""

    @property
    def model(self) -> str:
        return f"{self.dcfg.provider}/{self.dcfg.model}"

    def ask(
        self, state: Any, questions: dict[str, dict[str, Any]], *, label: str
    ) -> DecisionResult:
        """One request: every question evaluated in parallel against ``state``."""
        if self._unavailable:
            return DecisionResult(error=self._unavailable)
        started = time.perf_counter()
        if self._sdk_client is None:
            try:
                self._sdk_client = _build_sdk_client(self.dcfg)
            except DecisionUnavailable as exc:
                self._unavailable = str(exc)
                logger.warning("Camada de decisao desligada neste processo: %s", exc)
                return DecisionResult(error=self._unavailable)
        try:
            import typesafe_sdk as sdk

            try:
                response = self._sdk_client.system_one(state, _typed_questions(questions))
            except sdk.TypeSafeError as exc:
                raise DecisionUnavailable(f"{type(exc).__name__}: {exc}") from exc
        except DecisionUnavailable as exc:
            logger.warning("Camada de decisao indisponivel (%s): %s", label, exc)
            return DecisionResult(error=str(exc), latency_ms=_elapsed_ms(started))

        input_tokens = int(response.usage.input_tokens or 0)
        _record_usage(self.dcfg, self.model, input_tokens, label)
        return DecisionResult(
            answers={
                name: answer.model_dump(mode="json") for name, answer in response.answers.items()
            },
            input_tokens=input_tokens,
            latency_ms=_elapsed_ms(started),
        )


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _record_usage(dcfg: DecisionConfig, model: str, input_tokens: int, label: str) -> None:
    from zettel.usage import record_llm

    # Output tokens are free on this model; the price comes from config because
    # LiteLLM does not know it and would report $0.
    record_llm(
        model=model,
        tokens_in=input_tokens,
        tokens_out=0,
        cost_usd=input_tokens * dcfg.input_price_per_mtok / 1_000_000,
        label=f"jev:{label}",
    )


_CLIENTS: dict[tuple, DecisionClient] = {}


def get_decision_client(cfg: AppConfig) -> DecisionClient:
    """One client per configuration, reused across calls (keeps the HTTP pool)."""
    dcfg = cfg.decision
    key = (dcfg.provider, dcfg.model, dcfg.base_url, dcfg.timeout_s, dcfg.max_retries)
    client = _CLIENTS.get(key)
    if client is None:
        client = _CLIENTS[key] = DecisionClient(dcfg)
    return client
