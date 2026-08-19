"""Wspólna zakładka 🛈 informacji o wydaniu."""

from __future__ import annotations

import discord

from settings import SOURCE_EMOJIS
from shared import (
    aoty_score_value,
    load_release_variables,
    must_hear_title_marker,
    score_color,
    score_icon,
    set_aoty_footer,
)


DETAILS_BUTTON = "🛈"


def _details_value(value: object) -> str:
    text = str(value or "").strip()
    return text if text and text not in {"?", "Brak danych", "None"} else "—"


def _source_prefix(variables, section: str, value: object) -> str:
    if _details_value(value) == "—":
        return ""
    source = str(variables.metadata_sources.get(section) or "").casefold()
    emoji = SOURCE_EMOJIS.get(source or "aoty")
    return f"{emoji} " if emoji else ""


async def build_release_details_embed(
    item: dict,
    *,
    username: str | None = None,
    author_icon_url: str | None = None,
) -> discord.Embed:
    """Renderuj pełne informacje z SQLite i ich źródła."""

    variables = await load_release_variables(item, username=username, missing="—")
    lines = [
        f"{_source_prefix(variables, 'score', variables.aoty_user_score)}"
        f"**AOTY User Score:** "
        f"{aoty_score_value(variables.aoty_user_score, variables.ratings_count)}",
        f"{_source_prefix(variables, 'score', variables.ratings_count)}"
        f"**Ratings:** {variables.ratings_count}",
        f"{_source_prefix(variables, 'release_date', variables.release_date)}"
        f"**Release date:** {variables.release_date}",
        f"{_source_prefix(variables, 'tracklist', variables.duration)}"
        f"**Duration:** {variables.duration}",
        f"{_source_prefix(variables, 'format', variables.album_format)}"
        f"**Format:** {variables.album_format}",
        f"{_source_prefix(variables, 'labels', variables.labels_text)}"
        f"**Label:** {variables.labels_text}",
        f"{_source_prefix(variables, 'genres', variables.genres_text)}"
        f"**Genre:** {_details_value(variables.genres_text)}",
        f"{_source_prefix(variables, 'genres', variables.secondary_genres_text)}"
        f"**Secondary genres:** {_details_value(variables.secondary_genres_text)}",
        f"{_source_prefix(variables, 'vibes', variables.vibes_text)}"
        f"**Vibes:** {_details_value(variables.vibes_text)}",
    ]
    ranking_year = _details_value(variables.ranking_year)
    if ranking_year == "—":
        ranking_year = _details_value(variables.year)
    lines.append(
        f"{_source_prefix(variables, 'ranking', variables.year_ranking_text)}"
        f"**{ranking_year if ranking_year != '—' else 'Year'} Ratings:** "
        f"{_details_value(variables.year_ranking_text)}"
    )

    musicbrainz_data = variables.source_data.get("musicbrainz") or {}
    if musicbrainz_data:
        for key, label in (
            ("musicbrainz_release_id", "MusicBrainz release ID"),
            ("musicbrainz_release_group_id", "MusicBrainz release-group ID"),
        ):
            value = _details_value(musicbrainz_data.get(key))
            if value != "—":
                lines.append(f"{SOURCE_EMOJIS['musicbrainz']} **{label}:** `{value}`")
        country = _details_value(musicbrainz_data.get("release_country"))
        if country != "—":
            lines.append(f"{SOURCE_EMOJIS['musicbrainz']} **Release country:** {country}")

    lastfm_data = variables.source_data.get("lastfm") or {}
    if lastfm_data:
        for key, label in (("listeners_count", "Last.fm listeners"), ("playcount", "Last.fm scrobbles")):
            value = _details_value(lastfm_data.get(key))
            if value != "—":
                lines.append(f"{SOURCE_EMOJIS['lastfm']} **{label}:** {value}")

    embed = discord.Embed(
        title=(
            f"{DETAILS_BUTTON} {variables.display_artist} — {variables.display_album}"
            f" {must_hear_title_marker(variables)}".rstrip()
        ),
        url=variables.url or None,
        description="\n".join(lines),
        color=score_color(variables.score or variables.aoty_user_score),
    )
    if variables.cover:
        embed.set_thumbnail(url=variables.cover)
    if username:
        embed.set_author(
            name=f"{username}  •  {variables.date}",
            url=f"https://www.albumoftheyear.org/user/{username}/",
            icon_url=author_icon_url,
        )
    footer = (
        f"AOTY • {score_icon(variables.score)[1:]} {variables.score or 'NR'}"
        if username
        else "AOTY"
    )
    set_aoty_footer(embed, footer)
    return embed
