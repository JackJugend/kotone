"""Operator-only manual SQLite edits through Discord."""

from __future__ import annotations

import asyncio
import re

import discord

from database import DB
from formats import RATING_FORMATS
from rating_import import normalized_text
from settings import GUILD_ID, USERS, is_operator_discord_id


def _split_values(value: str | None) -> list[str]:
    return [
        item.strip()
        for item in re.split(r"[,;]\s*", str(value or ""))
        if item.strip()
    ]


def _parse_aoty_links(value: str | None) -> dict[str, object]:
    """Parse only the optional AOTY label link from the manual field."""

    result: dict[str, object] = {}
    for part in re.split(r"\s*;\s*", str(value or "")):
        if "=" not in part:
            continue
        key, raw = part.split("=", 1)
        key = key.strip().casefold().replace("-", "_")
        raw = raw.strip()
        if not raw:
            continue
        if key in {"label", "label_url"}:
            result["label_url"] = raw
    return result


def _parse_track_scores(value: str | None) -> list[str]:
    """Return optional AOTY track scores in the exact supplied order."""

    scores: list[str] = []
    for raw in re.split(r"[,;\s]+", str(value or "").strip()):
        if not raw:
            continue
        try:
            score = int(raw)
        except ValueError as exc:
            raise ValueError(f"Nieprawidlowa ocena utworu: {raw}.") from exc
        if not 0 <= score <= 100:
            raise ValueError(f"Nieprawidlowa ocena utworu: {raw}.")
        scores.append(str(score))
    return scores


def _parse_track_score_fields(value: str | None) -> tuple[str | None, str | None]:
    """Rozdziel wspólne pole Discorda na oceny AOTY i oceny użytkownika.

    Discord pozwala na maksymalnie 25 pól opcji.  Zamiast osobnej opcji
    ``user_track_scores`` używamy więc składni ``aoty:...;user:...``.
    Zwykła lista bez prefiksu zachowuje dotychczasowe znaczenie i oznacza
    oceny AOTY.
    """

    raw = str(value or "").strip()
    if not raw:
        return None, None
    parts = [part.strip() for part in re.split(r"\s*;\s*", raw) if part.strip()]
    aoty: str | None = None
    user: str | None = None
    plain: list[str] = []
    for part in parts:
        match = re.match(r"^(aoty|a|user|kotone|u)\s*:\s*(.*)$", part, re.I)
        if not match:
            plain.append(part)
            continue
        target, scores = match.groups()
        if target.casefold() in {"user", "kotone", "u"}:
            user = scores.strip()
        else:
            aoty = scores.strip()
    if plain:
        if aoty is not None:
            raise ValueError("track_scores: nie mieszaj listy bez prefiksu z aoty:.")
        aoty = ";".join(plain)
    return aoty, user


def _looks_like_tracklist(value: str | None) -> bool:
    """Recognise pasted numbered rows before they can become fake genres."""

    return bool(
        re.search(r"(?:^|\n|\s)\d{1,3}\s*[.)]\s*(?:\[.+?\]\(|\S)", str(value or ""))
    )


def _parse_tracklist(
    value: str | None,
    track_scores: str | None = None,
) -> list[dict]:
    """Parse compact or copied AOTY rows into release-track rows.

    Accepted copied form: ``1 [Title](https://...)3:12 96``. The final
    score is optional, therefore a public tracklist may be saved without
    any AOTY per-track ratings.
    """

    text = str(value or "").strip()
    if not text:
        return []
    # A slash-command field occasionally arrives as a single wrapped line.
    # Splitting before every numbered item lets both pasted AOTY rows and
    # simple ``1. Track`` lists stay structured.
    rows = [
        row.strip()
        for row in re.split(r"\n|;|(?=\s+\d{1,3}\s*[.)]\s+)", text)
        if row.strip()
    ]
    tracks: list[dict] = []
    for index, row in enumerate(rows, start=1):
        copied = re.match(
            r"^\s*(?P<number>\d+)\s*[.)]?\s*"
            r"\[(?P<title>.+?)\]\((?P<url>https?://[^)]+)\)\s*"
            r"(?P<duration>\d{1,2}:\d{2})?\s*"
            r"(?P<user_score>\d{1,3})?\s*$",
            row,
        )
        if copied:
            user_score = copied.group("user_score")
            if user_score is not None and not 0 <= int(user_score) <= 100:
                raise ValueError(f"Nieprawidlowa ocena utworu: {user_score}.")
            tracks.append(
                {
                    "number": int(copied.group("number")),
                    "title": copied.group("title").strip(),
                    "url": copied.group("url").strip(),
                    "duration": copied.group("duration") or None,
                    "user_score": user_score,
                    "disc": None,
                }
            )
            continue

        plain = re.match(
            r"^\s*(?P<number>\d+)\s*[.)]?\s*"
            r"(?P<title>.+?)"
            r"(?P<duration>\d{1,2}:\d{2})?\s*"
            r"(?P<user_score>\d{1,3})?\s*$",
            row,
        )
        if plain and plain.group("duration"):
            user_score = plain.group("user_score")
            if user_score is not None and not 0 <= int(user_score) <= 100:
                raise ValueError(f"Nieprawidlowa ocena utworu: {user_score}.")
            tracks.append(
                {
                    "number": int(plain.group("number")),
                    "title": plain.group("title").rstrip(" :").strip(),
                    "duration": plain.group("duration") or None,
                    "user_score": user_score,
                    "disc": None,
                }
            )
            continue

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
        if user_score is not None:
            try:
                if not 0 <= int(user_score) <= 100:
                    raise ValueError
            except ValueError as exc:
                raise ValueError(f"Nieprawidlowa ocena utworu: {user_score}.") from exc
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
    supplied_scores = _parse_track_scores(track_scores)
    if supplied_scores:
        if len(supplied_scores) != len(tracks):
            raise ValueError(
                "Liczba track_scores musi być taka sama jak liczba utworów w tracklist."
            )
        for track, score in zip(tracks, supplied_scores, strict=True):
            track["user_score"] = score
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
        "labels": any(
            payload.get(key) not in (None, "", [], {})
            for key in ("label", "labels", "label_url")
        ),
        "genres": any(
            payload.get(key) not in (None, "", [], {})
            for key in (
                "genres",
                "genre_urls",
                "secondary_genres",
                "secondary_genre_urls",
            )
        ),
        "vibes": payload.get("vibes") not in (None, "", [], {}),
        "ranking": any(
            payload.get(key) not in (None, "")
            for key in ("year_ranking_text", "all_time_ranking")
        ),
        "duration": payload.get("duration") not in (None, ""),
        "tracklist": payload.get("tracklist") not in (None, "", [], {}),
    }


def _format_label(value: str | None) -> str | None:
    """Normalize the slash-command choice to Kotone's display label."""

    raw = str(value or "").strip()
    if not raw:
        return None
    for key, info in RATING_FORMATS.items():
        candidates = {key.casefold(), info["slug"].casefold(), info["label"].casefold()}
        if raw.casefold() in candidates:
            return info["label"]
    return raw


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
        album_id="AOTY album_id; nowy identyfikator utworzy pozycję ręczną",
        username="kotone user; wymagany tylko dla oceny/review/like",
        rating_score="kotone user rating 0-100",
        rating_date="kotone user rating date, np. 20.08.2026",
        liked="kotone user like",
        review="kotone user review",
        artist="Nazwa artysty",
        album="Nazwa release",
        cover="URL okładki",
        release_date="Release date/year, np. 31.01.2021",
        format="Release format",
        duration="Release duration, np. 45:12",
        label="Main label",
        genres="Gatunki po przecinku",
        secondary_genres="Secondary gatunki po przecinku",
        aoty_links="Opcjonalnie: label=URL AOTY",
        aoty_score="AOTY User score",
        ratings_count="AOTY ratings count",
        critic_score="AOTY Critic Score",
        critic_reviews_count="AOTY critic reviews count",
        year_ranking="Ranking roczny, np. #12",
        all_time_ranking="Ranking all-time, np. #48",
        must_hear="Status Must Hear",
        tracklist=(
            "Utwory: 1. Tytuł 2. Tytuł; czas trwania opcjonalnie"
        ),
        track_scores=(
            "Oceny utworów: zwykła lista=AOTY; osobno aoty:89,93;user:90,89"
        ),
    )
    @discord.app_commands.autocomplete(
        album_id=dbmanual_album_autocomplete,
        username=dbmanual_user_autocomplete,
    )
    @discord.app_commands.choices(
        format=[
            discord.app_commands.Choice(name=info["label"], value=key)
            for key, info in RATING_FORMATS.items()
        ],
        must_hear=[
            discord.app_commands.Choice(name="Must Hear users", value="users"),
            discord.app_commands.Choice(name="Must Hear critics", value="critics"),
            discord.app_commands.Choice(name="Must Hear both", value="both"),
            discord.app_commands.Choice(name="Brak Must Hear", value="none"),
        ],
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
        format: str | None = None,
        duration: str | None = None,
        label: str | None = None,
        genres: str | None = None,
        secondary_genres: str | None = None,
        aoty_links: str | None = None,
        aoty_score: str | None = None,
        ratings_count: str | None = None,
        critic_score: str | None = None,
        critic_reviews_count: str | None = None,
        year_ranking: str | None = None,
        all_time_ranking: str | None = None,
        must_hear: str | None = None,
        tracklist: str | None = None,
        track_scores: str | None = None,
    ) -> None:
        if interaction.guild_id != GUILD_ID:
            await interaction.response.send_message(
                "Ta komenda działa tylko na skonfigurowanym serwerze.",
                ephemeral=True,
            )
            return
        if not is_operator_discord_id(getattr(interaction.user, "id", None)):
            await interaction.response.send_message(
                "Nie masz uprawnień do `/dbmanual`.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        changed_parts: list[str] = []
        try:
            await asyncio.to_thread(DB.backup_if_due, force=True)
            album_id = str(album_id or "").strip()
            # Operators may paste a complete AOTY link. Persist its stable
            # numeric ID instead of creating a release keyed by the URL.
            album_id_match = re.search(r"/album/(\d+)(?:[-/]|$)", album_id)
            if album_id_match:
                album_id = album_id_match.group(1)
            if not album_id:
                raise ValueError("Podaj album_id.")

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

            # Older Discord clients can keep a stale field order after a
            # command update.  A pasted numbered list is never a genre; move
            # it to the intended field instead of displaying it on the home
            # card as one long fake genre line.
            moved_tracklist = False
            if not tracklist and _looks_like_tracklist(genres):
                tracklist, genres = genres, None
                moved_tracklist = True
            aoty_track_scores, user_track_scores = _parse_track_score_fields(track_scores)
            parsed_tracklist = _parse_tracklist(tracklist, aoty_track_scores)
            personal_track_scores = _parse_track_scores(user_track_scores)
            if personal_track_scores and not username:
                raise ValueError("Podaj username dla user_track_scores.")
            existing_release = await asyncio.to_thread(DB.get_release_details, album_id)
            tracks_for_user = parsed_tracklist or list(
                (existing_release or {}).get("tracklist") or []
            )
            if personal_track_scores and len(personal_track_scores) != len(tracks_for_user):
                raise ValueError(
                    "Liczba user_track_scores musi być taka sama jak liczba utworów w tracklist."
                )
            release_payload = {
                "artist": artist,
                "album": album,
                "cover": cover,
                "release_date": release_date,
                "album_format": _format_label(format),
                "duration": duration,
                "label": label,
                "labels": _split_values(label),
                "genres": _split_values(genres),
                "secondary_genres": _split_values(secondary_genres),
                "user_score": aoty_score,
                "ratings_count": ratings_count,
                "critic_score": critic_score,
                "critic_reviews_count": critic_reviews_count,
                "year_ranking_text": year_ranking,
                "all_time_ranking": all_time_ranking,
                "must_hear_kind": must_hear,
                "tracklist": parsed_tracklist,
            }
            release_payload.update(_parse_aoty_links(aoty_links))
            release_payload = {
                key: value
                for key, value in release_payload.items()
                if value not in (None, "", [], {})
            }
            if release_payload or existing_release is None:
                # Bare album_id creates a small manual placeholder. The
                # operator can fill the remaining fields in another call.
                if existing_release is None:
                    release_payload.setdefault("artist", "Nieznany artysta")
                    release_payload.setdefault("album", f"Album #{album_id}")
                # Ranking roczny i all-time są niezależnymi wartościami.
                # Oznaczamy dokładnie podane pola, aby np. ustawienie samego
                # rankingu rocznego nie wyczyściło wcześniej zapisanego all-time.
                ranking_fields = [
                    key
                    for key in (
                        "ranking_year",
                        "year_ranking",
                        "year_ranking_text",
                        "all_time_ranking",
                    )
                    if key in release_payload
                ]
                if ranking_fields:
                    release_payload["_ranking_fields"] = ranking_fields
                release_payload["_section_complete"] = _section_complete(release_payload)
                await asyncio.to_thread(
                    DB.manual_update_release_details,
                    album_id,
                    release_payload,
                    actor=str(interaction.user.id),
                )
                changed_parts.append(
                    "nowa pozycja" if existing_release is None else "release metadata"
                )
                if moved_tracklist:
                    changed_parts.append("tracklista przeniesiona z błędnego pola")

            if personal_track_scores:
                personal_tracks = [
                    {
                        "number": track.get("number"),
                        "title": track.get("title"),
                        "score": score,
                    }
                    for track, score in zip(
                        tracks_for_user,
                        personal_track_scores,
                        strict=True,
                    )
                ]
                saved_tracks = await asyncio.to_thread(
                    DB.manual_update_user_track_ratings,
                    username,
                    album_id,
                    personal_tracks,
                )
                changed_parts.append(
                    f"oceny tracklisty {saved_tracks['username']}={saved_tracks['count']}"
                )

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
