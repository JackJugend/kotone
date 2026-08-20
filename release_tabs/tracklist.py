"""Wspólna zakładka ☰ tracklisty i ocen utworów."""

from __future__ import annotations

import discord

import aoty
from release_tabs.common import (
    apply_release_identity,
    paginate_description_lines,
    release_tab_title,
)
from services import DATA
from settings import USERS
from shared import (
    build_release_variables,
    score_color,
    score_or_nr,
    set_aoty_footer,
    user_avatar_emoji,
)
from ui_constants import TRACKLIST_BUTTON


def _track_key(value: object) -> str:
    return "".join(character.casefold() for character in str(value or "") if character.isalnum())


def _track_number(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _rating_track_maps(rows: list[dict]) -> tuple[dict[int, dict], dict[str, dict]]:
    by_number: dict[int, dict] = {}
    by_title: dict[str, dict] = {}
    for row in rows:
        number = _track_number(row.get("number"))
        title_key = _track_key(row.get("title"))
        if number is not None:
            by_number.setdefault(number, row)
        if title_key:
            by_title.setdefault(title_key, row)
    return by_number, by_title


def _build_tracklist_embed(
    variables,
    description: str,
    *,
    username: str | None,
    author_icon_url: str | None,
    page_number: int | None = None,
    page_count: int | None = None,
) -> discord.Embed:
    """Zbuduj jedną stronę tracklisty, zachowując wspólną identyfikację."""

    title = release_tab_title(TRACKLIST_BUTTON, variables)
    if page_count and page_count > 1 and page_number:
        title = f"{title}  •  {page_number}/{page_count}"
    embed = discord.Embed(
        title=title,
        url=variables.url or None,
        description=description,
        color=score_color(variables.score or variables.aoty_user_score),
    )
    apply_release_identity(
        embed,
        variables,
        username=username,
        author_icon_url=author_icon_url,
    )
    set_aoty_footer(embed, f"Tracklist  •  {variables.album_format}  •  oceny użytkowników")
    return embed


async def build_combined_tracklist_embeds(
    item: dict,
    *,
    username: str | None = None,
    author_icon_url: str | None = None,
) -> list[discord.Embed]:
    """Połącz trwałą tracklistę z ocenami użytkowników z configu."""

    item = dict(item or {})
    album_id = str(item.get("album_id") or aoty.extract_album_id(item.get("url")) or "")
    if album_id:
        item["album_id"] = album_id
    try:
        details = await DATA.get_release_details(item, allow_network=False)
    except Exception:
        details = {}

    variables = build_release_variables(item, details, missing="—")
    public_tracks = [dict(track) for track in variables.tracklist]
    personal: dict[str, list[dict]] = {}
    for configured_username in USERS[:25]:
        cached = DATA.cached_rating(configured_username, album_id) if album_id else None
        stored_tracks = DATA.cached_user_track_ratings(configured_username, album_id) if album_id else []
        if cached is None:
            personal[configured_username] = stored_tracks
            continue
        selected = cached
        needs_refresh = bool(
            not cached.get("detail_complete")
            or (cached.get("has_track_ratings") and not cached.get("track_ratings"))
        )
        if needs_refresh:
            try:
                selected = await DATA.get_user_rating_for_album(
                    configured_username,
                    album_id,
                    item.get("url") or details.get("url"),
                    item.get("release_format") or item.get("album_format") or details.get("album_format"),
                    fallback_limit=20,
                    user_release_url=cached.get("review_url"),
                    album_title=item.get("album") or item.get("title"),
                    require_detail=True,
                    allow_network=False,
                )
            except Exception:
                selected = cached
        selected_tracks = list(selected.get("track_ratings") or [])
        personal[configured_username] = selected_tracks if selected_tracks or selected.get("detail_complete") else stored_tracks

    personal_maps = {configured_username: _rating_track_maps(rows) for configured_username, rows in personal.items()}
    merged: list[dict] = []
    seen_numbers: set[int] = set()
    seen_titles: set[str] = set()
    for position, track in enumerate(public_tracks, start=1):
        number = _track_number(track.get("number"))
        title_key = _track_key(track.get("title"))
        merged.append({**track, "_display_number": number or position})
        if number is not None:
            seen_numbers.add(number)
        if title_key:
            seen_titles.add(title_key)
    for rows in personal.values():
        for row in rows:
            number = _track_number(row.get("number"))
            title_key = _track_key(row.get("title"))
            if (number is not None and number in seen_numbers) or (title_key and title_key in seen_titles):
                continue
            merged.append({"number": number, "title": row.get("title"), "duration": None, "user_score": None, "_display_number": number or len(merged) + 1})
            if number is not None:
                seen_numbers.add(number)
            if title_key:
                seen_titles.add(title_key)

    lines: list[str] = []
    for track in merged:
        number = _track_number(track.get("number"))
        display_number = track.get("_display_number") or "—"
        title = str(track.get("title") or "Nieznany utwór")
        title_key = _track_key(title)
        duration = f" `{track.get('duration')}`" if track.get("duration") else ""
        url = track.get("url")
        title_text = f"[{title}]({url})" if url else title
        public_score = track.get("user_score")
        scores = [
            f"<:aoty:1539095897084924004> {score_or_nr(public_score)}"
            if str(public_score or "").strip()
            else "<:aoty:1539095897084924004> **—**"
        ]
        for configured_username in USERS[:25]:
            by_number, by_title = personal_maps.get(configured_username, ({}, {}))
            row = (by_number.get(number) if number is not None else None) or by_title.get(title_key)
            personal_score = (row or {}).get("score")
            scores.append(
                f"{user_avatar_emoji} {score_or_nr(personal_score)}"
            )
        lines.append(f"**{display_number}.** {title_text}{duration}\n" + " • ".join(scores))

    descriptions = paginate_description_lines(
        lines or ["Brak tracklisty w kotone."]
    )
    return [
        _build_tracklist_embed(
            variables,
            description,
            username=username,
            author_icon_url=author_icon_url,
            page_number=index,
            page_count=len(descriptions),
        )
        for index, description in enumerate(descriptions, start=1)
    ]


async def build_combined_tracklist_embed(
    item: dict,
    *,
    username: str | None = None,
    author_icon_url: str | None = None,
) -> discord.Embed:
    """Kompatybilny renderer pierwszej strony tracklisty."""

    return (await build_combined_tracklist_embeds(
        item,
        username=username,
        author_icon_url=author_icon_url,
    ))[0]
