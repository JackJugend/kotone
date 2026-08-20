"""Wspólne, małe elementy prezentacji zakładek wydania."""

from __future__ import annotations

import discord

from shared import ReleaseVariables, must_hear_title_marker


MISSING_VALUE = "—"


def display_value(value: object) -> str:
    """Ujednolić brakującą wartość bez zmiany prawdziwego zera."""

    text = str(value if value is not None else "").strip()
    if not text or text.casefold() in {"?", "brak danych", "none"}:
        return MISSING_VALUE
    return text


def trim_description(text: str, limit: int = 4000) -> str:
    """Przytnij opis do bezpiecznego limitu embeda Discord."""

    normalized = str(text or "").strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"


def release_tab_title(symbol: str, variables: ReleaseVariables) -> str:
    """Zbuduj identyczny tytuł dla każdej dodatkowej zakładki."""

    marker = must_hear_title_marker(variables)
    return (
        f"{symbol} {variables.display_artist} — {variables.display_album} "
        f"{marker}"
    ).rstrip()


def apply_release_identity(
    embed: discord.Embed,
    variables: ReleaseVariables,
    *,
    username: str | None,
    author_icon_url: str | None,
) -> None:
    """Dodać tę samą okładkę i autora do info, tracklisty i recenzji."""

    if variables.cover:
        embed.set_thumbnail(url=variables.cover)
    if username:
        embed.set_author(
            name=f"{username}  •  {variables.date}",
            url=f"https://www.albumoftheyear.org/user/{username}/",
            icon_url=author_icon_url,
        )

