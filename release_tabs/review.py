"""Wspólna zakładka ✎ recenzji dla wszystkich komend wydania."""

from __future__ import annotations

import discord

from release_tabs.common import (
    apply_release_identity,
    release_tab_title,
    trim_description,
)
from services import DATA
from shared import (
    build_release_variables,
    score_color,
    score_or_nr,
)
from ui_constants import REVIEW_BUTTON


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
    variables.date = str(extra.get("date") or item.get("date") or "—")

    embed = discord.Embed(
        title=release_tab_title(REVIEW_BUTTON, variables),
        url=extra.get("review_url") or item.get("url"),
        description=trim_description(review_text),
        color=score_color(score),
    )
    apply_release_identity(
        embed,
        variables,
        username=username,
        author_icon_url=author_icon_url,
    )
    embed.set_footer(text=f"AOTY • {score_or_nr(score)}")
    return embed
