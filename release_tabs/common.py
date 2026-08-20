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


def paginate_description_lines(
    lines: list[str],
    *,
    limit: int = 3600,
) -> list[str]:
    """Podziel kompletne wiersze na bezpieczne strony embeda.

    Nie przycinamy tracklisty ani szczegółów tylko dlatego, że Discord ma
    limit opisu.  Strona kończy się pomiędzy wierszami; wyjątkowo długi
    pojedynczy wiersz dzielimy dopiero jako ostatnią deskę ratunku.
    """

    pages: list[str] = []
    current: list[str] = []
    current_length = 0

    def finish_page() -> None:
        nonlocal current, current_length
        text = "\n".join(current).strip()
        if text:
            pages.append(text)
        current = []
        current_length = 0

    for raw_line in lines:
        line = str(raw_line or "")
        # Discord cannot accept one enormous line either.  Most content never
        # enters this branch, but it keeps a malformed external value safe.
        fragments = [line[index:index + limit] for index in range(0, len(line), limit)] or [""]
        for fragment in fragments:
            addition = len(fragment) + (1 if current else 0)
            if current and current_length + addition > limit:
                finish_page()
            current.append(fragment)
            current_length += len(fragment) + (1 if len(current) > 1 else 0)

    finish_page()
    return pages or [""]


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
