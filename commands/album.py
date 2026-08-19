import asyncio
import re
import time
from collections.abc import Mapping

import discord
import requests

import aoty

from services import DATA
from display_utils import display_romanized_name
from settings import USERS
from presence_cache import PRESENCE_CACHE
from shared import (
    aoty_score_or_missing,
    load_release_variables,
    must_hear_title_marker,
    rating_flags_text,
    release_year_suffix,
    score_color,
    score_icon,
    score_or_nr,
    set_aoty_footer,
)
from views import AlbumRatingView

DISCOGRAPHY_CACHE_TTL = 900


def _clean_presence_text(value) -> str | None:
    value = str(value or "").strip()
    if not value or value.casefold() in {"none", "unknown", "n/a"}:
        return None
    return value


def _presence_asset_text(activity) -> str | None:
    """Read the optional album caption exposed by compatible music RPCs."""

    for source in (activity, getattr(activity, "assets", None)):
        if source is None:
            continue
        value = getattr(source, "large_text", None)
        if value:
            return _clean_presence_text(value)
        if isinstance(source, Mapping):
            value = source.get("large_text")
            if value:
                return _clean_presence_text(value)
    return None


def _music_from_presence(
    member,
    *,
    cached_activities: tuple[object, ...] = (),
) -> tuple[str, str, str] | None:
    """Return artist, album and source from structured Discord music presence.

    Spotify is the only standardized Discord music activity. Other programs
    (Tidal, Apple Music clients, Last.fm integrations, etc.) are accepted only
    when their RPC exposes both an artist/state and an album asset caption; a
    song title alone is never guessed to be an album.
    """

    activities = tuple(getattr(member, "activities", ()) or ())
    if not activities:
        activities = cached_activities

    for activity in activities:
        if isinstance(activity, discord.Spotify):
            artists = list(getattr(activity, "artists", ()) or ())
            artist = _clean_presence_text(", ".join(map(str, artists)))
            album = _clean_presence_text(getattr(activity, "album", None))
            if artist and album:
                return artist, album, "Spotify"

        # Common custom music RPC layout (including the one shown in Discord's
        # "Listening to Music" card): details is "Artist - Track" while state
        # is the album.  Only accept it for an actual listening activity, so a
        # generic game/status string can never be mistaken for an album.
        details = _clean_presence_text(getattr(activity, "details", None))
        state = _clean_presence_text(getattr(activity, "state", None))
        if (
            getattr(activity, "type", None) == discord.ActivityType.listening
            and details
            and state
        ):
            parts = re.split(r"\s+(?:-|–|—)\s+", details, maxsplit=1)
            artist = _clean_presence_text(parts[0] if len(parts) == 2 else None)
            if artist:
                return (
                    artist,
                    # Discord exposes the cover tooltip as ``large_text``.
                    # Prefer it: custom RPCs often use state for a different
                    # field, while the artwork caption is consistently album.
                    _presence_asset_text(activity) or state,
                    _clean_presence_text(getattr(activity, "name", None))
                    or "Music RPC",
                )

        album = _presence_asset_text(activity)
        artist = state
        if artist and artist.casefold().startswith("by "):
            artist = artist[3:].strip() or None
        source = _clean_presence_text(getattr(activity, "name", None))
        if artist and album and source:
            return artist, album, source

    return None


def setup_album_command(tree: discord.app_commands.CommandTree):
    discography_cache = {}

    async def _discography_for(artist_value, *, prefer_cached=False):
        key = str(artist_value or "").strip().casefold()
        now = time.monotonic()
        cached = discography_cache.get(key)

        if (
            not prefer_cached
            and cached
            and now - cached[0] < DISCOGRAPHY_CACHE_TTL
        ):
            return cached[1], cached[2]

        artist_info, discography = await DATA.get_artist_discography(
            artist_value,
            prefer_cached=prefer_cached,
        )
        if not artist_info or not discography:
            return None, None

        # Only memoize a live discography. SQLite is already fast and durable;
        # caching its partial artist view here could prevent a later live
        # supplement when the AOTY challenge ends.
        if discography.get("source") != "SQLite cache":
            payload = (now, artist_info, discography)
            discography_cache[key] = payload

            choice_value = artist_info.get("value")
            if choice_value:
                discography_cache[choice_value.casefold()] = payload

            artist_name = artist_info.get("name")
            if artist_name:
                discography_cache[artist_name.casefold()] = payload

        return artist_info, discography

    async def artist_autocomplete(interaction: discord.Interaction, current: str):
        if not current or len(current.strip()) < 2:
            return []

        results = await DATA.search_artists(current, limit=10)

        return [
            discord.app_commands.Choice(
                name=display_romanized_name(item["name"])[:100],
                value=item["value"][:100],
            )
            for item in results[:10]
        ]

    async def album_autocomplete(interaction: discord.Interaction, current: str):
        artist_value = getattr(interaction.namespace, "artist", None)
        if not artist_value:
            return []

        try:
            # Known artists/releases come straight from SQLite. Only artists
            # absent from the configured-user cache need a live lookup.
            artist_info, discography = DATA.cached_artist_discography(
                artist_value
            )
            if not artist_info or not discography:
                artist_info, discography = await _discography_for(artist_value)
            if not artist_info or not discography:
                return []

            releases = discography.get("releases", [])
            if current.strip():
                releases = [
                    release
                    for _, release in aoty.rank_artist_releases(releases, current)
                ]

            choices = []

            for release in releases[:25]:
                title = display_romanized_name(release.get("title", ""))
                suffix = [
                    value
                    for value in (release.get("year"), release.get("album_format"))
                    if value
                ]
                choice_name = f"{title} ({' · '.join(suffix)})" if suffix else title
                choices.append(
                    discord.app_commands.Choice(
                        name=choice_name[:100],
                        value=("aoty_album:" + str(release["album_id"]))[:100],
                    )
                )

            return choices
        except Exception:
            return []

    @tree.command(
        name="album",
        description="Pokazuje informacje danego release z AOTY.",
    )
    @discord.app_commands.describe(
        artist="Artysta (opcjonalnie)",
        album="Wydanie (opcjonalnie)",
    )
    @discord.app_commands.autocomplete(
        artist=artist_autocomplete,
        album=album_autocomplete,
    )
    async def album_command(
        interaction: discord.Interaction,
        artist: str | None = None,
        album: str | None = None,
    ):
        await interaction.response.defer()

        artist = str(artist or "").strip()
        album = str(album or "").strip()
        if bool(artist) != bool(album):
            await interaction.followup.send(
                "❌ Podaj jednocześnie **artist** i **album**, albo zostaw oba pola puste."
            )
            return

        if not artist:
            member = interaction.user
            if interaction.guild is not None:
                member = interaction.guild.get_member(interaction.user.id) or member
            presence = _music_from_presence(
                member,
                cached_activities=PRESENCE_CACHE.activities_for(interaction.user.id),
            )
            if presence is None:
                await interaction.followup.send(
                    "❌ Nie widzę aktywnego albumu w Twoim Rich Presence.")
                return
            artist, album, source = presence
            print(f"[ALBUM] Rich Presence ({source}): {artist} — {album}")

        try:
            # A cached match is authoritative enough to select the requested
            # release. If it cannot match, live AOTY may supplement it.
            artist_info, discography = DATA.cached_artist_discography(artist)
            ranked = (
                aoty.rank_artist_releases(
                    discography.get("releases", []),
                    album,
                )
                if discography
                else []
            )
            direct_choice = str(album).startswith("aoty_album:")
            cached_match = bool(
                ranked
                and (direct_choice or ranked[0][0] >= 0.28)
            )

            if not cached_match:
                artist_info, discography = await _discography_for(artist)

            if not artist_info or not discography:
                await interaction.followup.send(
                    f"❌ Nie znaleziono artysty **{artist}** na AOTY."
                )
                return

            ranked = aoty.rank_artist_releases(
                discography.get("releases", []),
                album,
            )

            if not ranked:
                await interaction.followup.send(
                    f"❌ Nie znaleziono wydania **{album}** u tego artysty."
                )
                return

            match_score, release = ranked[0]

            if not direct_choice and match_score < 0.28:
                await interaction.followup.send(
                    f"❌ Nie znaleziono wystarczająco podobnego wydania do **{album}**."
                )
                return

        except aoty.AOTYRateLimit:
            await interaction.followup.send(
                "⚠️ AOTY chwilowo ogranicza liczbę zapytań."
            )
            return
        except requests.RequestException as exc:
            await interaction.followup.send(
                f"❌ Błąd połączenia z AOTY: `{exc}`"
            )
            return
        except Exception as exc:
            await interaction.followup.send(
                f"❌ Błąd: `{type(exc).__name__}: {exc}`"
            )
            return

        artist_name = discography.get("artist") or artist_info["name"]
        release_item = dict(release)
        release_item["artist"] = artist_name
        release_item["album"] = release.get("title")
        release_item["release_format"] = release.get(
            "album_format"
        )

        variables = await load_release_variables(
            release_item,
        )

        # Keep the selected card hydrated for every shared action tab.  The
        # main embed and its buttons must see the same SQLite-first values.
        release_item.update(
            {
                "artist": variables.artist,
                "album": variables.album,
                "url": variables.album_url or release_item.get("url"),
                # Preserve the provider artwork for later tabs. ``cover`` is
                # potentially a generated Must Hear image URL for Discord.
                "cover": variables.raw_cover or release_item.get("cover"),
                "release_format": variables.album_format,
            }
        )
        artist_url = (
            variables.artist_url
            or discography.get("url")
            or artist_info.get("url")
        )
        icon_url = discography.get("image")
        if artist_url:
            release_item["artist_url"] = artist_url

        description_lines = []
        if variables.genres:
            description_lines.append(variables.all_genres_text.title())
        if variables.secondary_genres:
            description_lines.append(variables.secondary_genres_text.title())

        embed = discord.Embed(
            title=(
                f"{must_hear_title_marker(variables)} **{variables.display_album}**"
                f"{release_year_suffix(variables.year)}"
            ),
            url=variables.url,
            description="\n".join(description_lines) or None,
            color=score_color(variables.aoty_user_score),
        )

        aoty_score = aoty_score_or_missing(
            variables.aoty_user_score,
            variables.ratings_count,
        )

        embed.add_field(
                name="AOTY",
                value=f"**{aoty_score}**",
                inline=True,
        )

        # Pełny zapis SQLite jest źródłem domyślnym. AOTY jest używane tylko,
        # gdy danego usera/wydania nie ma jeszcze w trwałym cache.
        rating_infos: dict[str, dict] = {}

        for username in USERS[:25]:
            try:
                rating_info = await DATA.get_user_rating_for_album(
                    username,
                    release_item["album_id"],
                    release_item.get("url"),
                    variables.album_format,
                    fallback_limit=20,
                    user_release_url=None,
                    album_title=variables.album,
                    require_detail=False,
                    allow_network=False,
                )
            except aoty.AOTYRateLimit:
                rating_info = {"score": None, "source": "rate limit"}
            except Exception:
                rating_info = {"score": None}

            rating_infos[username] = rating_info
            rating = rating_info.get("score")
            flags = rating_flags_text(rating_info)
            flags_text = f"  {flags}" if flags else ""

            if rating is not None:
                rating_value = f"{score_icon(rating)} {rating}{flags_text}"
            else:
                rating_value = f"{score_or_nr(None)}{flags_text}"

            embed.add_field(
                name=username,
                value=rating_value,
                inline=True,
            )
            await asyncio.sleep(0.15)

        # The artist line above an /album card is display-only, so it must use
        # the same romanization rule as the title, autocomplete and views.
        author = {"name": display_romanized_name(artist_name)}
        if artist_url:
            author["url"] = artist_url
        if icon_url:
            author["icon_url"] = icon_url
        embed.set_author(**author)

        if variables.cover:
            embed.set_thumbnail(url=variables.cover)

        set_aoty_footer(
            embed,
            f"{variables.album_format}  •  {variables.release_date}  •  "
            f"{variables.labels_text}",
        )

        view = AlbumRatingView(
            main_embed=embed,
            release_item=release_item,
            usernames=USERS[:25],
            rating_infos=rating_infos,
        )

        message = await interaction.followup.send(
            embed=embed,
            view=view,
            wait=True,
        )
        view.bind_message(message)
