"""Shared variables and Discord helpers used by every command.

Najważniejszy element to ReleaseVariables. Wszystkie dane wydania, które
wcześniej były kopiowane osobno do /last, /recent, /album i monitora,
są teraz przygotowywane tutaj. Nazwy zmiennych zostały zachowane.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import discord

from display_utils import display_romanized_name


def score_color(score: Any) -> discord.Color:
    try:
        value = int(score)
    except (TypeError, ValueError):
        return discord.Color.light_grey()

    if value == 100:
        return discord.Color.from_rgb(66, 255, 255)
    if value >= 90:
        return discord.Color.from_rgb(28, 242, 155)
    if value >= 80:
        return discord.Color.from_rgb(18, 215, 98)
    if value >= 70:
        return discord.Color.from_rgb(51, 255, 0)
    if value >= 60:
        return discord.Color.from_rgb(174, 255, 0)
    if value >= 50:
        return discord.Color.from_rgb(255, 229, 0)
    if value >= 40:
        return discord.Color.from_rgb(255, 157, 0)
    if value >= 30:
        return discord.Color.from_rgb(255, 91, 0)
    if value >= 20:
        return discord.Color.from_rgb(255, 31, 15)
    if value >= 10:
        return discord.Color.from_rgb(140, 20, 20)
    return discord.Color.from_rgb(58, 10, 10)


def score_icon(score: Any) -> str:
    try:
        value = int(score)
    except (TypeError, ValueError):
        return "⚪"

    if value == 100:
        return "💎"
    if value >= 90:
        return "💚"
    if value >= 80:
        return "🟢"
    if value >= 70:
        return "🟢"
    if value >= 60:
        return "🟡"
    if value >= 50:
        return "🟡"
    if value >= 40:
        return "🟠"
    if value >= 30:
        return "🟠"
    if value >= 20:
        return "🔴"
    if value >= 10:
        return "❓"
    return "⚫"


@dataclass(slots=True)
class ReleaseVariables:
    # Dane oceny użytkownika.
    score: Any = None
    artist: str = ""
    album: str = ""
    display_artist: str = ""
    display_album: str = ""
    date: str = ""
    url: str = ""
    cover: str | None = None
    release_format: str | None = None
    album_id: str = ""

    # Linki AOTY.
    artist_url: str = ""
    album_url: str = ""

    # Średnia AOTY.
    user_score: Any = None
    aoty_user_score: Any = None
    ratings_count: Any = None

    # Wydanie.
    release_date: Any = None
    year: Any = None
    album_format: Any = None

    # Label.
    label: Any = None
    labels: list[str] = field(default_factory=list)
    labels_text: Any = None

    # Primary genres.
    genres: list[str] = field(default_factory=list)
    genres_text: Any = None
    main_genre: Any = None
    other_genres: Any = None
    other_genres_text: Any = None
    all_genres_text: Any = None

    # Secondary genres.
    secondary_genres: list[str] = field(default_factory=list)
    secondary_genres_text: Any = None

    # Vibes.
    vibes: list[str] = field(default_factory=list)
    vibes_text: Any = None

    # Rankingi.
    ranking_year: Any = None
    year_ranking: Any = None
    year_ranking_text: Any = None

    # Tracklista AOTY.
    tracklist: list[dict] = field(default_factory=list)
    tracklist_text: Any = None

    # Metadane konkretnej oceny usera.
    has_review: bool = False
    has_track_ratings: bool = False
    liked: bool = False
    review_url: str | None = None
    review_text: str | None = None
    track_ratings: list[dict] = field(default_factory=list)


@dataclass(slots=True)
class ProfileVariables:
    """Standard profile variables shared by /profile and future commands."""

    username: str = ""
    display_username: str = ""
    avatar: str | None = None
    profile_url: str = ""
    ratings_count: str = "0"
    reviews_count: str = "0"
    lists_count: str = "0"
    following_count: str = "0"
    followers_count: str = "0"
    average_rating: float | None = None
    average_rating_text: str = "Brak danych"
    favorite_kind: str | None = None
    favorites: list[dict] = field(default_factory=list)
    favorite_albums: list[dict] = field(default_factory=list)
    favorite_artists: list[dict] = field(default_factory=list)
    recent_ratings: list[dict] = field(default_factory=list)


def build_profile_variables(
    profile: dict | None,
    fallback_username: str = "",
) -> ProfileVariables:
    """Normalize profile defaults in one place, like ReleaseVariables."""

    profile = profile or {}
    display_username = str(
        profile.get("username")
        or fallback_username
        or "Nieznany użytkownik"
    )

    return ProfileVariables(
        username=str(fallback_username or display_username),
        display_username=display_username,
        avatar=profile.get("avatar"),
        profile_url=str(profile.get("url") or ""),
        ratings_count=str(profile.get("ratings_count") or "0"),
        reviews_count=str(profile.get("reviews_count") or "0"),
        lists_count=str(profile.get("lists_count") or "0"),
        following_count=str(profile.get("following_count") or "0"),
        followers_count=str(profile.get("followers_count") or "0"),
        average_rating=profile.get("average_rating"),
        average_rating_text=str(
            profile.get("average_rating_text")
            or "Brak danych"
        ),
        favorite_kind=profile.get("favorite_kind"),
        favorites=list(profile.get("favorites") or []),
        favorite_albums=list(profile.get("favorite_albums") or []),
        favorite_artists=list(profile.get("favorite_artists") or []),
        recent_ratings=list(profile.get("recent_ratings") or []),
    )


def build_release_variables(
    item: dict | None,
    details: dict | None = None,
    *,
    missing: str = "Brak danych",
) -> ReleaseVariables:
    """Build every standard release variable in one place.

    Commands should use ``variables.<name>``. This keeps all names stable and
    removes duplicated default/fallback logic from individual command files.
    """

    item = item or {}
    details = details or {}

    artist = str(item.get("artist") or details.get("artist") or "Nieznany artysta")
    album = str(item.get("album") or item.get("title") or details.get("album") or "Nieznane wydanie")

    genres = list(details.get("genres") or [])
    secondary_genres = list(details.get("secondary_genres") or [])
    vibes = list(details.get("vibes") or [])
    labels = list(details.get("labels") or [])
    tracklist = list(details.get("tracklist") or [])

    main_genre = genres[0] if genres else missing

    if len(genres) > 1:
        other_genres = ", ".join(genres[1:])
        other_genres_text = other_genres
        all_genres_text = f"**{main_genre}**, {other_genres_text}"
    elif genres:
        other_genres = missing
        other_genres_text = missing
        all_genres_text = f"**{main_genre}**"
    else:
        other_genres = missing
        other_genres_text = missing
        all_genres_text = missing

    user_score = details.get("user_score") or missing

    return ReleaseVariables(
        score=item.get("score"),
        artist=artist,
        album=album,
        display_artist=display_romanized_name(artist),
        display_album=display_romanized_name(album),
        date=str(item.get("date") or missing),
        url=str(item.get("url") or details.get("url") or ""),
        cover=item.get("cover") or details.get("cover"),
        release_format=item.get("release_format") or details.get("album_format"),
        album_id=str(item.get("album_id") or ""),
        artist_url=str(
            item.get("artist_url")
            or details.get("artist_url")
            or ""
        ),
        album_url=str(
            item.get("url")
            or details.get("url")
            or ""
        ),
        user_score=user_score,
        aoty_user_score=user_score,
        ratings_count=details.get("ratings_count") or missing,
        release_date=details.get("release_date") or missing,
        year=details.get("year") or item.get("year") or missing,
        album_format=(
            details.get("album_format")
            or item.get("release_format")
            or item.get("album_format")
            or missing
        ),
        label=details.get("label") or missing,
        labels=labels,
        labels_text=details.get("labels_text") or (", ".join(labels) if labels else missing),
        genres=genres,
        genres_text=details.get("genres_text") or (", ".join(genres) if genres else missing),
        main_genre=main_genre,
        other_genres=other_genres,
        other_genres_text=other_genres_text,
        all_genres_text=all_genres_text,
        secondary_genres=secondary_genres,
        secondary_genres_text=(
            details.get("secondary_genres_text")
            or (", ".join(secondary_genres) if secondary_genres else missing)
        ),
        vibes=vibes,
        vibes_text=details.get("vibes_text") or (", ".join(vibes) if vibes else missing),
        ranking_year=details.get("ranking_year") or missing,
        year_ranking=details.get("year_ranking") or missing,
        year_ranking_text=details.get("year_ranking_text") or missing,
        tracklist=tracklist,
        tracklist_text=details.get("tracklist_text") or missing,
        has_review=bool(item.get("has_review")),
        has_track_ratings=bool(item.get("has_track_ratings")),
        liked=bool(item.get("liked")),
        review_url=item.get("review_url"),
        review_text=item.get("review_text"),
        track_ratings=list(item.get("track_ratings") or []),
    )



async def load_release_variables(
    item: dict | None,
    *,
    missing: str = "Brak danych",
) -> ReleaseVariables:
    """The single public-release details path used across the whole bot.

    This prevents /last, /recent, /album and monitor notifications from
    silently using different ratings_count/default logic.
    """
    item = item or {}
    details = {}

    url = item.get("url")

    if url:
        import aoty

        try:
            details = await asyncio.to_thread(
                aoty.get_album_details,
                url,
            )
        except aoty.AOTYRateLimit:
            details = {}
        except Exception:
            details = {}

    return build_release_variables(
        item,
        details,
        missing=missing,
    )


def rating_flags_text(item_or_variables: dict | ReleaseVariables | None) -> str:
    if item_or_variables is None:
        return ""

    if isinstance(item_or_variables, ReleaseVariables):
        has_review = item_or_variables.has_review
        has_track_ratings = item_or_variables.has_track_ratings
        liked = item_or_variables.liked
    else:
        has_review = bool(item_or_variables.get("has_review"))
        has_track_ratings = bool(item_or_variables.get("has_track_ratings"))
        liked = bool(item_or_variables.get("liked"))

    flags: list[str] = []

    if has_review:
        flags.append("✎")
    if has_track_ratings:
        flags.append("☷")
    if liked:
        flags.append("♥︎")

    return " ".join(flags)


async def username_autocomplete(interaction: discord.Interaction, current: str):
    """Shared AOTY username autocomplete used by /last /recent /profile."""
    current = str(current or "").strip()

    if len(current) < 2:
        return []

    # Import lokalny zapobiega cyklicznemu importowi przy starcie bota.
    import aoty

    try:
        results = await asyncio.to_thread(
            aoty.search_aoty_users,
            current,
            10,
        )
    except Exception:
        return []

    return [
        discord.app_commands.Choice(
            name=str(item.get("name") or item["username"])[:100],
            value=str(item["username"])[:100],
        )
        for item in results[:10]
    ]
