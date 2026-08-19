"""Lekki, globalny cache emoji ocen Kotone.

Moduł nie importuje Discorda ani SQLite. Dzięki temu helpery prezentacji mogą
bezpiecznie z niego korzystać w każdej komendzie, a synchronizator odświeża
mapę dopiero po udanym zapisie emoji przez API Discorda.
"""

from __future__ import annotations

from collections.abc import Mapping

_SCORE_EMOJIS: dict[int, str] = {}


def set_score_emojis(values: Mapping[int, str]) -> None:
    """Atomically replace cached markup with validated 0–100 entries."""

    global _SCORE_EMOJIS
    _SCORE_EMOJIS = {
        int(score): str(markup)
        for score, markup in values.items()
        if -1 <= int(score) <= 100 and str(markup).startswith("<:")
    }


def score_emoji(score: int | None) -> str | None:
    """Return the uploaded tile for a rating, if it is already available."""

    return _SCORE_EMOJIS.get(int(score)) if score is not None else _SCORE_EMOJIS.get(-1)


def score_emoji_count() -> int:
    """Expose sync progress for logs and diagnostics."""

    return len(_SCORE_EMOJIS)
