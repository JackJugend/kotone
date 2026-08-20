"""Operator-only manual SQLite edits through Discord."""

from __future__ import annotations

import asyncio
import re

import discord

from database import DB
from rating_import import normalized_text
from settings import GUILD_ID, USERS, is_operator_discord_id


def _split_values(value: str | None) -> list[str]:
    return [
        item.strip()
        for item in re.split(r"[,;]\s*", str(value or ""))
        if item.strip()
    ]


def _parse_tracklist(value: str | None) -> list[dict]:
    """Parse compact operator input into release_tracks rows."""

    text = str(value or "").strip()
    if not text:
        return []
    rows = [row.strip() for row in re.split(r"\n|;", text) if row.strip()]
    tracks: list[dict] = []
    for index, row in enumerate(rows, start=1):
        parts = [part.strip() for part in row.split("|")]
        head = parts[0] if parts else ""
        number = index
        title = head
        match = re.match(r"^(?P<number>\d+)[\.\)]?\s+(?P<title>.+)$", head)
        if match:
            number = int(match.group("number"))
            title = match.group("title").strip()
        duration = parts[1] if len(parts) >= 2 and parts[1] else None
        user_score = parts[2] if len(parts) >= 3 and parts[2] else None
        disc = parts[3] if len(parts) >= 4 and parts[3] else None
        if title:
            tracks.append(
                {
                    "number": number,
                    "title": title,
                    "duration": duration,
                    "user_score": user_score,
                    "disc": disc,
                }
            )
    return tracks


def _section_complete(payload: dict) -> dict[str, bool]:
    return {
        "score": any(
            payload.get(key) not in (None, "", [], {})
            for key in (
                "user_score",
                "ratings_count",
                "critic_score",
                "critic_reviews_count",
            )
        ),
        "release_date": payload.get("release_date") not in (None, ""),
        "format": payload.get("album_format") not in (None, ""),
        "labels": any(payload.get(key) not in (None, "", [], {}) for key in ("label", "labels")),
        "genres": any(
            payload.get(key) not in (None, "", [], {})
            for key in ("genres", "secondary_genres")
        ),
        "vibes": payload.get("vibes") not in (None, "", [], {}),
        "ranking": payload.get("year_ranking_text") not in (None, ""),
        "tracklist": payload.get("tracklist") not in (None, "", [], {}),
    }


async def dbmanual_album_autocomplete(
    interaction: discord.Interaction,
    current: str,
):
    if not is_operator_discord_id(getattr(interaction.user, "id", None)):
        return []
    needle = normalized_text(current)
    choices = []
    seen = set()
    for username in USERS:
        rows = await asyncio.to_thread(DB.get_analytics_rows, username)
        for row in rows:
            album_id = str(row.get("album_id") or "").strip()
            if not album_id or album_id in seen:
                continue
            label = " - ".join(
                value
                for value in (
                    str(row.get("artist") or "").strip(),
                    str(row.get("album") or "").strip(),
                )
                if value
            )
            if needle and needle not in normalized_text(f"{album_id} {label}"):
                continue
            seen.add(album_id)
            choices.append(
                discord.app_commands.Choice(
                    name=(f"{label} [{album_id}]" if label else f"Album #{album_id}")[:100],
                    value=album_id[:100],
                )
            )
            if len(choices) >= 25:
                return choices
    return choices


async def dbmanual_user_autocomplete(
    interaction: discord.Interaction,
    current: str,
):
    if not is_operator_discord_id(getattr(interaction.user, "id", None)):
        return []
    needle = normalized_text(current)
    return [
        discord.app_commands.Choice(name=username, value=username)
        for username in USERS
        if not needle or needle in normalized_text(username)
    ][:25]


def setup_dbmanual_command(tree: discord.app_commands.CommandTree) -> None:
    @tree.command(
        name="dbmanual",
        description="Operatorsko dodaje lub zmienia dane zapisane w SQLite.",
    )
    @discord.app_commands.describe(
        album_id="AOTY album_id istniejacej pozycji w bazie Kotone",
        username="User z configu; wymagany tylko dla oceny/review/like",
        rating_score="Ocena usera z configu 0-100",
        rating_date="Data oceny usera, np. 20.08.2026",
        liked="Ustawia like usera",
        review="Ustawia albo nadpisuje review usera",
        artist="Nazwa artysty",
        album="Nazwa wydania",
        cover="URL okladki",
        release_date="Data wydania, np. 31.01.2021",
        year="Rok wydania",
        format="Format, np. LP / EP / Single",
        label="Glowna wytwornia",
        genres="Gatunki po przecinku",
        secondary_genres="Drugorzedne gatunki po przecinku",
        vibes="Vibes po przecinku",
        aoty_score="AOTY User Score",
        ratings_count="Liczba ocen AOTY",
        critic_score="Critic Score",
        critic_reviews_count="Liczba recenzji krytykow",
        year_ranking="Ranking roczny, np. #12",
        must_hear="Reczny status Must Hear",
        tracklist="Tracki: 1. Title | 3:12 | 96 | disc; 2. Title...",
    )
    @discord.app_commands.autocomplete(
        album_id=dbmanual_album_autocomplete,
        username=dbmanual_user_autocomplete,
    )
    async def dbmanual_command(
        interaction: discord.Interaction,
        album_id: str,
        username: str | None = None,
        rating_score: int | None = None,
        rating_date: str | None = None,
        liked: bool | None = None,
        review: str | None = None,
        artist: str | None = None,
        album: str | None = None,
        cover: str | None = None,
        release_date: str | None = None,
        year: str | None = None,
        format: str | None = None,
        label: str | None = None,
        genres: str | None = None,
        secondary_genres: str | None = None,
        vibes: str | None = None,
        aoty_score: str | None = None,
        ratings_count: str | None = None,
        critic_score: str | None = None,
        critic_reviews_count: str | None = None,
        year_ranking: str | None = None,
        must_hear: bool | None = None,
        tracklist: str | None = None,
    ) -> None:
        if interaction.guild_id != GUILD_ID:
            await interaction.response.send_message(
                "Ta komenda dziala tylko na skonfigurowanym serwerze.",
                ephemeral=True,
            )
            return
        if not is_operator_discord_id(getattr(interaction.user, "id", None)):
            await interaction.response.send_message(
                "Nie masz uprawnien do `/dbmanual`.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        changed_parts: list[str] = []
        try:
            await asyncio.to_thread(DB.backup_if_due, force=True)

            if rating_score is not None or rating_date:
                if not username:
                    raise ValueError("Podaj username dla oceny usera.")
                if rating_score is None:
                    raise ValueError("Podaj rating_score razem z rating_date.")
                result = await asyncio.to_thread(
                    DB.manual_update_rating_score,
                    username,
                    album_id,
                    int(rating_score),
                    rating_date=rating_date,
                )
                changed_parts.append(f"ocena {result['username']}={result['score']}")

            if liked is not None:
                if not username:
                    raise ValueError("Podaj username dla like.")
                result = await asyncio.to_thread(
                    DB.manual_update_rating_detail,
                    username,
                    album_id,
                    "like_on" if liked else "like_off",
                )
                changed_parts.append(f"like {result['username']}={'tak' if liked else 'nie'}")

            if review is not None:
                if not username:
                    raise ValueError("Podaj username dla review.")
                action = "review_remove" if not str(review).strip() else "review_set"
                result = await asyncio.to_thread(
                    DB.manual_update_rating_detail,
                    username,
                    album_id,
                    action,
                    review_text=review,
                )
                changed_parts.append(f"review {result['username']}")

            release_payload = {
                "artist": artist,
                "album": album,
                "cover": cover,
                "release_date": release_date,
                "year": year,
                "album_format": format,
                "label": label,
                "labels": _split_values(label),
                "genres": _split_values(genres),
                "secondary_genres": _split_values(secondary_genres),
                "vibes": _split_values(vibes),
                "user_score": aoty_score,
                "ratings_count": ratings_count,
                "critic_score": critic_score,
                "critic_reviews_count": critic_reviews_count,
                "year_ranking_text": year_ranking,
                "must_hear": must_hear,
                "tracklist": _parse_tracklist(tracklist),
            }
            release_payload = {
                key: value
                for key, value in release_payload.items()
                if value not in (None, "", [], {})
            }
            if release_payload:
                release_payload["_section_complete"] = _section_complete(release_payload)
                await asyncio.to_thread(
                    DB.manual_update_release_details,
                    album_id,
                    release_payload,
                    actor=str(interaction.user.id),
                )
                changed_parts.append("release metadata")

        except Exception as exc:
            await interaction.followup.send(
                f"❌ `/dbmanual` nie zapisalo zmian: `{type(exc).__name__}: {exc}`",
                ephemeral=True,
            )
            return

        if not changed_parts:
            await interaction.followup.send(
                "❌ Podaj przynajmniej jedno pole do zapisania.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            "✅ Zapisano: " + ", ".join(changed_parts) + ".",
            ephemeral=True,
        )
