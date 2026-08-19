"""Globalny cache małych, transparentnych ikon flag wydania."""

from __future__ import annotations

from collections.abc import Mapping

_STATUS_EMOJIS: dict[str, str] = {}


def set_status_emojis(values: Mapping[str, str]) -> None:
    """Replace cached bot-owned status emoji markup."""

    global _STATUS_EMOJIS
    _STATUS_EMOJIS = {
        str(key).casefold(): str(markup)
        for key, markup in values.items()
        if str(key).casefold() in {"like", "tracklist", "review"}
        and str(markup).startswith("<:")
    }


def status_emoji(key: str) -> str | None:
    """Return an uploaded flag icon, if its initial sync has finished."""

    return _STATUS_EMOJIS.get(str(key).casefold())
