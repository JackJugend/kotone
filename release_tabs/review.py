"""Wspólna zakładka ✎ recenzji dla wszystkich komend wydania."""

from __future__ import annotations

import discord

from services import DATA
from shared import (
    build_release_variables,
    must_hear_title_marker,
    score_color,
    score_or_nr,
)


def _trim_description(text: str, limit: int = 4000) -> str:
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def build_review_embed(
    username: str,
    item: dict,
    extra: dict,
    *,
    author_icon_url: str | None = None,
) -> discord.Embed:
    """Renderuj recenzję z danych już zapisanych przez bota."""

    score = extra.get("score") or item.get("score")
    review_text = extra.get("review_text") or "Brak recenzji."
    hydrated = DATA.release_with_cached_details(item)
    album_id = str(hydrated.get("album_id") or "").strip()
    cached = DATA.cached_release_details(album_id) if album_id else {}
    variables = build_release_variables(hydrated, cached or {})
    marker = must_hear_title_marker(variables)

    embed = discord.Embed(
        title=(
            f"✎ {variables.display_artist} — {variables.display_album}"
            f" {marker}".rstrip()
        ),
        url=extra.get("review_url") or item.get("url"),
        description=_trim_description(review_text),
        color=score_color(score),
    )
    embed.set_author(
        name=f"{username}  •  {extra.get('date') or item.get('date') or '—'}",
        url=f"https://www.albumoftheyear.org/user/{username}/",
        icon_url=author_icon_url,
    )
    if variables.cover:
        embed.set_thumbnail(url=variables.cover)
    embed.set_footer(text=f"AOTY • {score_or_nr(score)}")
    return embed
