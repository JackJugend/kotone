"""Wspólna zakładka informacji o wydaniu."""

from __future__ import annotations

from collections.abc import Mapping

import discord

from release_tabs.common import (
    MISSING_VALUE,
    apply_release_identity,
    display_value,
    release_tab_title,
)
from shared import (
    ReleaseVariables,
    aoty_score_value,
    country_flag_emoji,
    load_release_variables,
    score_color,
    score_or_nr,
    set_aoty_footer,
    source_emoji,
)
from ui_constants import DETAILS_BUTTON


# ---------------------------------------------------------------------------
# Formatowanie pojedynczych wierszy
# ---------------------------------------------------------------------------

def _source_prefix(
    variables: ReleaseVariables,
    section: str,
    value: object,
) -> str:
    """Zwróć emoji źródła tylko przy istniejącej wartości."""

    if display_value(value) == MISSING_VALUE:
        return ""
    source = str(variables.metadata_sources.get(section) or "aoty").casefold()
    emoji = source_emoji(source)
    return f"{emoji} " if emoji else ""


def _detail_line(
    variables: ReleaseVariables,
    *,
    section: str,
    label: str,
    value: object,
) -> str:
    return (
        f"{_source_prefix(variables, section, value)}"
        f"**{label}:** {display_value(value)}"
    )


def _provider_line(source: str, label: str, value: object) -> str | None:
    rendered = display_value(value)
    if rendered == MISSING_VALUE:
        return None
    prefix = source_emoji(source)
    return f"{prefix} **{label}:** {rendered}".lstrip()


# ---------------------------------------------------------------------------
# Sekcje opisu
# ---------------------------------------------------------------------------

def _score_section(variables: ReleaseVariables) -> list[str]:
    score = aoty_score_value(
        variables.aoty_user_score,
        variables.ratings_count,
    )
    return [
        _detail_line(
            variables,
            section="score",
            label="AOTY User Score",
            value=score,
        ),
        _detail_line(
            variables,
            section="score",
            label="Ratings",
            value=variables.ratings_count,
        ),
    ]


def _genres_section(variables: ReleaseVariables) -> list[str]:
    return [
        _detail_line(
            variables,
            section="genres",
            label="Genre",
            value=variables.genres_text,
        ),
        _detail_line(
            variables,
            section="genres",
            label="Secondary genres",
            value=variables.secondary_genres_text,
        ),
        _detail_line(
            variables,
            section="vibes",
            label="Vibes",
            value=variables.vibes_text,
        ),
    ]


def _release_section(variables: ReleaseVariables) -> list[str]:
    return [
        _detail_line(
            variables,
            section="release_date",
            label="Release date",
            value=variables.release_date,
        ),
        _detail_line(
            variables,
            section="duration",
            label="Duration",
            value=variables.duration,
        ),
        _detail_line(
            variables,
            section="format",
            label="Format",
            value=variables.album_format,
        ),
        _detail_line(
            variables,
            section="labels",
            label="Label",
            value=variables.labels_text,
        ),
    ]


def _ranking_section(variables: ReleaseVariables) -> list[str]:
    ranking_year = display_value(variables.ranking_year)
    if ranking_year == MISSING_VALUE:
        ranking_year = display_value(variables.year)
    label = f"{ranking_year} ratings" if ranking_year != MISSING_VALUE else "Year ratings"
    lines = [
        _detail_line(
            variables,
            section="ranking",
            label=label,
            value=variables.year_ranking_text,
        )
    ]
    if display_value(variables.all_time_ranking) != MISSING_VALUE:
        lines.append(
            _detail_line(
                variables,
                section="ranking",
                label="All-time ratings",
                value=variables.all_time_ranking,
            )
        )
    return lines


def _musicbrainz_section(source_data: Mapping[str, object]) -> list[str]:
    country_code = source_data.get("release_country")
    country = country_flag_emoji(country_code) or display_value(country_code)
    rows = (
        ("MusicBrainz release ID", source_data.get("musicbrainz_release_id")),
        (
            "MusicBrainz release-group ID",
            source_data.get("musicbrainz_release_group_id"),
        ),
        ("Release country", country),
    )
    return [
        line
        for label, value in rows
        if (line := _provider_line("musicbrainz", label, value))
    ]


def _lastfm_section(source_data: Mapping[str, object]) -> list[str]:
    rows = (
        ("Listeners", source_data.get("listeners_count")),
        ("Scrobbles", source_data.get("playcount")),
    )
    return [
        line
        for label, value in rows
        if (line := _provider_line("lastfm", label, value))
    ]


def _discogs_section(source_data: Mapping[str, object]) -> list[str]:
    """Show only durable Discogs identity, not a redundant source summary."""

    return [
        line
        for label, value in (
            ("Discogs release ID", source_data.get("discogs_release_id")),
        )
        if (line := _provider_line("discogs", label, value))
    ]


def _description_lines(variables: ReleaseVariables) -> list[str]:
    """Złożyć sekcje w jednym, czytelnym miejscu i stałej kolejności."""

    sections = [
        _score_section(variables),
        _genres_section(variables),
        _release_section(variables),
        _ranking_section(variables),
        _musicbrainz_section(variables.source_data.get("musicbrainz") or {}),
        _lastfm_section(variables.source_data.get("lastfm") or {}),
        _discogs_section(variables.source_data.get("discogs") or {}),
    ]

    lines: list[str] = []
    for section in sections:
        if not section:
            continue
        if lines:
            lines.append("")
        lines.extend(section)
    return lines


# ---------------------------------------------------------------------------
# Publiczny renderer zakładki
# ---------------------------------------------------------------------------

async def build_release_details_embed(
    item: dict,
    *,
    username: str | None = None,
    author_icon_url: str | None = None,
) -> discord.Embed:
    """Renderuj pełne informacje z SQLite wraz z ich źródłami."""

    variables = await load_release_variables(
        item,
        username=username,
        missing=MISSING_VALUE,
    )
    embed = discord.Embed(
        title=release_tab_title(DETAILS_BUTTON, variables),
        url=variables.url or None,
        description="\n".join(_description_lines(variables)),
        color=score_color(variables.score or variables.aoty_user_score),
    )
    apply_release_identity(
        embed,
        variables,
        username=username,
        author_icon_url=author_icon_url,
    )

    footer = f"AOTY • {score_or_nr(variables.score)}" if username else "AOTY"
    set_aoty_footer(embed, footer)
    return embed
