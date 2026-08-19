"""Wspólny model danych i helpery prezentacji dla wszystkich komend.

``ReleaseVariables`` jest jednym źródłem prawdy dla danych wydania z SQLite,
AOTY i MusicBrainz. Każda komenda powinna najpierw zbudować ten obiekt zamiast
samodzielnie wybierać pola z różnych słowników. Dzięki temu fallbacki,
brakujące wartości, daty, gatunki i flagi wyglądają tak samo w całym bocie.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import discord

from display_utils import display_genres, display_release_date, display_romanized_name
from must_hear import marked_cover_url, must_hear_album, must_hear_kind, numeric_count
from settings import (
    AOTY_ICON_ATTACHMENT,
    MUST_HEAR_EMOJIS,
    USERS,
)


def set_aoty_footer(embed: discord.Embed, text: str) -> None:
    """Apply Kotone's shared AOTY footer asset consistently."""

    embed.set_footer(
        text=str(text),
        icon_url=AOTY_ICON_ATTACHMENT,
    )


def score_color(score: Any) -> discord.Color:
    value = _score_number(score)
    if value is None:
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
    value = _score_number(score)
    if value is None:
        return "\⚪"

    if value == 100:
        return "\💎"
    if value >= 90:
        return "\💚"
    if value >= 80:
        return "\🟢"
    if value >= 70:
        return "\🟢"
    if value >= 60:
        return "\🟡"
    if value >= 50:
        return "\🟡"
    if value >= 40:
        return "\🟠"
    if value >= 30:
        return "\🟠"
    if value >= 20:
        return "\🔴"
    if value >= 10:
        return "\❓"
    return "\⚫"


def _score_number(score: Any) -> int | None:
    """Return the whole-number part of a score, if it is numeric.

    AOTY sometimes exposes a decimal score (for example ``78.2``), while
    Kotone displays scores as whole numbers everywhere.  Centralizing that
    conversion keeps the text, colour and score emoji in sync.
    """

    text = str(score or "").strip().replace(",", ".")
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return None


def _whole_score_text(score: Any) -> str:
    """Format a real score without a decimal part; leave non-scores intact."""

    value = _score_number(score)
    return str(value) if value is not None else str(score or "").strip()


def score_value_or_nr(score: Any) -> str:
    """Return a personal rating value, using NR only for a true no-rating."""

    text = str(score or "").strip()
    if not text or text.casefold() in {"nr", "n/r", "—", "?"}:
        return "NR"
    return _whole_score_text(text)


def score_or_nr(score: Any) -> str:
    """Render every missing rating consistently as a white ``NR`` marker."""

    value = score_value_or_nr(score)
    return f"{score_icon(None)} NR" if value == "NR" else f"{score_icon(value)} {value}"


def add_centered_inline_fields(
    embed: discord.Embed,
    fields: list[tuple[str, str]],
) -> None:
    """Append rating fields while centring incomplete three-column rows.

    Discord has a fixed three-column layout for inline embed fields.  Empty
    zero-width fields are structural spacers only; they do not add visible
    text or replace rating flags in a value.
    """

    visible = [(str(name), str(value)) for name, value in fields]
    remainder = len(visible) % 3
    spacer = ("\u200b", "\u200b")
    if remainder == 1:
        visible = [spacer, *visible, spacer]
    elif remainder == 2:
        visible = [*visible, spacer]
    for name, value in visible:
        embed.add_field(name=name, value=value, inline=True)


def score_or_missing(score: Any) -> str:
    """Render every missing aoty ratings info consistently as a white ``—`` marker."""

    text = str(score or "").strip()
    if not text or text.casefold() in {"nr", "n/r", "—", "?"}:
        return f"{score_icon(None)} —"
    value = _whole_score_text(text)
    return f"{score_icon(value)} {value}"


def aoty_score_value(score: Any, ratings_count: Any) -> str:
    """Render a missing public AOTY score without guessing.

    ``NR`` means AOTY explicitly reports zero user ratings.  When a ratings
    count exists but the score did not parse/cache, the honest value is ``—``:
    calling that state NR would falsely say that nobody rated the release.
    """

    text = str(score or "").strip()
    if text and text.casefold() not in {"nr", "n/r", "—", "?"}:
        return _whole_score_text(text)
    return "NR" if numeric_count(ratings_count) == 0 else "—"


def aoty_score_or_missing(score: Any, ratings_count: Any) -> str:
    """Score + icon for public AOTY values using the safe NR distinction."""

    value = aoty_score_value(score, ratings_count)
    return score_or_nr(None) if value == "NR" else score_or_missing(value)


def release_year_suffix(year: Any) -> str:
    """Return a title suffix only for a real cached release year."""

    text = str(year or "").strip()
    if not text or text.casefold() in {"—", "?", "brak danych", "none"}:
        return ""
    return f" ({text})"


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
    # ``raw_cover`` is durable provider artwork. ``cover`` can be a generated
    # Must Hear endpoint and must never be written back into SQLite/card data.
    raw_cover: str | None = None
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
    critic_score: Any = None
    critic_reviews_count: Any = None
    must_hear: bool = False

    # Wydanie.
    release_date: Any = None
    year: Any = None
    album_format: Any = None
    duration: Any = None

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
    metadata_sources: dict[str, str] = field(default_factory=dict)
    # Provider-specific IDs/counts remain separate from the shared fields
    # above.  They are used only by the details tab, never to replace AOTY.
    source_data: dict[str, dict] = field(default_factory=dict)

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
    average_rating_text: str = "—"
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
            or "—"
        ),
        favorite_kind=profile.get("favorite_kind"),
        favorites=list(profile.get("favorites") or []),
        favorite_albums=list(profile.get("favorite_albums") or []),
        favorite_artists=list(profile.get("favorite_artists") or []),
        recent_ratings=list(profile.get("recent_ratings") or []),
    )


def user_avatar_emoji(username: str) -> str:
    """Return the current cached custom emoji for a Kotone profile.

    The ID lives in SQLite because Discord replaces it whenever an AOTY
    avatar changes.  Keeping the lookup here gives all command embeds the
    same stable ``<:name:id>`` representation without copying IDs into code.
    A missing/unsynchronised emoji deliberately degrades to empty text.
    """

    try:
        # Lazy import keeps the shared display module free of startup cycles.
        from database import DB

        state = DB.get_avatar_emoji_state(username) or {}
        emoji_id = str(state.get("emoji_id") or "").strip()
        name = str(username or "").strip().casefold()
        return f"<:{name}:{emoji_id}>" if name and emoji_id else ""
    except Exception:
        return ""


def build_release_variables(
    item: dict | None,
    details: dict | None = None,
    *,
    missing: str = "—",
) -> ReleaseVariables:
    """Build every standard release variable in one place.

    Commands should use ``variables.<name>``. This keeps all names stable and
    removes duplicated default/fallback logic from individual command files.
    """

    item = item or {}
    details = details or {}

    artist = str(item.get("artist") or details.get("artist") or "Nieznany artysta")
    album = str(item.get("album") or item.get("title") or details.get("album") or "Nieznane wydanie")

    # A compact rating card and ``releases`` cache deliberately overlap.  Do
    # not treat a failed/partial AOTY enrichment as a blank release: the card
    # (often already hydrated from SQLite) remains a valid fallback for every
    # command and every tab.
    def value(name: str, default=None):
        candidate = details.get(name)
        if candidate not in (None, "", [], {}):
            return candidate
        candidate = item.get(name)
        return default if candidate in (None, "", [], {}) else candidate

    genres = display_genres(value("genres", []) or [])
    secondary_genres = display_genres(value("secondary_genres", []) or [])
    vibes = list(value("vibes", []) or [])
    labels = list(value("labels", []) or [])
    tracklist = list(value("tracklist", []) or [])

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

    user_score = value("user_score", missing)
    ratings_count = value("ratings_count", missing)
    critic_score = value("critic_score", missing)
    critic_reviews_count = value("critic_reviews_count", missing)
    album_id = str(item.get("album_id") or details.get("album_id") or "")
    # Prefer the durable release cover, but preserve a compact rating-card URL
    # when it is the only known artwork. The badge endpoint receives that URL
    # explicitly, so every tab can use the same Must Hear cover even before a
    # separate ``releases`` cache row exists.
    raw_cover = details.get("cover") or item.get("cover")
    must_hear = must_hear_album(
        user_score,
        ratings_count,
        critic_score,
        critic_reviews_count,
        album_id=album_id,
        official=details.get("must_hear"),
    )
    display_cover = (
        marked_cover_url(album_id, raw_cover)
        if must_hear
        else None
    ) or raw_cover

    def total_duration(tracklist_value: list[dict]) -> str | None:
        """Return an album runtime from cached track lengths when possible."""

        total_seconds = 0
        found = False
        for track in tracklist_value:
            raw = str((track or {}).get("duration") or "").strip()
            parts = raw.split(":")
            if not raw or not all(part.isdigit() for part in parts):
                continue
            try:
                seconds = 0
                for part in parts:
                    seconds = seconds * 60 + int(part)
            except ValueError:
                continue
            total_seconds += seconds
            found = True
        if not found:
            return None
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return (
            f"{hours}:{minutes:02d}:{seconds:02d}"
            if hours
            else f"{minutes}:{seconds:02d}"
        )

    return ReleaseVariables(
        score=item.get("score"),
        artist=artist,
        album=album,
        display_artist=display_romanized_name(artist),
        display_album=display_romanized_name(album),
        date=str(item.get("date") or missing),
        url=str(item.get("url") or details.get("url") or ""),
        raw_cover=raw_cover,
        cover=display_cover,
        release_format=item.get("release_format") or details.get("album_format"),
        album_id=album_id,
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
        ratings_count=ratings_count,
        critic_score=critic_score,
        critic_reviews_count=critic_reviews_count,
        must_hear=must_hear,
        release_date=display_release_date(value("release_date", missing), missing=missing),
        year=value("year", missing),
        album_format=(
            details.get("album_format")
            or item.get("release_format")
            or item.get("album_format")
            or missing
        ),
        duration=value("duration", total_duration(tracklist) or missing),
        label=value("label", missing),
        labels=labels,
        labels_text=value("labels_text", ", ".join(labels) if labels else missing),
        genres=genres,
        genres_text=", ".join(genres) if genres else missing,
        main_genre=main_genre,
        other_genres=other_genres,
        other_genres_text=other_genres_text,
        all_genres_text=all_genres_text,
        secondary_genres=secondary_genres,
        secondary_genres_text=(
            ", ".join(secondary_genres) if secondary_genres else missing
        ),
        vibes=vibes,
        vibes_text=value("vibes_text", ", ".join(vibes) if vibes else missing),
        ranking_year=value("ranking_year", missing),
        year_ranking=value("year_ranking", missing),
        year_ranking_text=value("year_ranking_text", missing),
        tracklist=tracklist,
        tracklist_text=details.get("tracklist_text") or missing,
        metadata_sources=dict(details.get("metadata_sources") or {}),
        source_data=dict(details.get("source_data") or {}),
        has_review=bool(item.get("has_review")),
        has_track_ratings=bool(item.get("has_track_ratings")),
        liked=bool(item.get("liked")),
        review_url=item.get("review_url"),
        review_text=item.get("review_text"),
        track_ratings=list(item.get("track_ratings") or []),
    )


def must_hear_title_marker(variables: ReleaseVariables) -> str:
    """Return the configured Must Hear emoji for a known release, or empty."""

    if not variables.must_hear:
        return ""
    kind = must_hear_kind(
        variables.aoty_user_score,
        variables.ratings_count,
        variables.critic_score,
        variables.critic_reviews_count,
    )
    return MUST_HEAR_EMOJIS[kind]


async def load_release_variables(
    item: dict | None,
    *,
    username: str | None = None,
    missing: str = "—",
) -> ReleaseVariables:
    """Build release variables through the shared cache/live service.

    AOTY release details for configured users are persisted in SQLite.
    MusicBrainz may persist only sections still missing from AOTY.
    """
    item = item or {}
    details = {}

    try:
        from services import DATA

        # Hydrate the compact card before attempting HTTP.  This makes SQLite
        # the global default regardless of which command renders the release.
        item = DATA.release_with_cached_details(item)

        details = await DATA.get_release_details(
            item,
            username=username,
            allow_network=False,
        )
    except Exception:
        # Release enrichment is optional. A rating should still render from the
        # data already present in the rating card / SQLite.
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
        flags.append("☰")
    if liked:
        flags.append("❤︎⁠")

    return " ".join(flags)


async def configured_username_autocomplete(
    interaction: discord.Interaction,
    current: str,
    *,
    limit: int = 10,
):
    """Zwróć użytkowników z configu bez kontaktu z AOTY."""
    current = str(current or "").strip()

    choices: list[discord.app_commands.Choice] = []
    seen = set()
    needle = current.casefold()

    for username in USERS:
        if needle not in username.casefold():
            continue
        choices.append(
            discord.app_commands.Choice(
                name=username[:100],
                value=username[:100],
            )
        )
        seen.add(username.casefold())

    return choices[: max(1, min(25, int(limit)))]


async def username_autocomplete(
    interaction: discord.Interaction,
    current: str,
):
    """Callback Discorda o wymaganej sygnaturze dla standardowych komend."""

    return await configured_username_autocomplete(interaction, current, limit=10)

