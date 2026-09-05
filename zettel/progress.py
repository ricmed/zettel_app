"""Presentation-neutral progress contracts shared by CLI and web callers."""

from __future__ import annotations

from typing import Protocol


class ProgressObserver(Protocol):
    def update(
        self,
        phase: str,
        message: str,
        *,
        current_item: str | None = None,
        current_index: int | None = None,
        total_items: int | None = None,
    ) -> None: ...


def report(
    observer: ProgressObserver | None,
    phase: str,
    message: str,
    *,
    current_item: str | None = None,
    current_index: int | None = None,
    total_items: int | None = None,
) -> None:
    """Notify an optional observer; existing CLI/domain calls remain unchanged."""
    if observer is not None:
        observer.update(
            phase,
            message,
            current_item=current_item,
            current_index=current_index,
            total_items=total_items,
        )
