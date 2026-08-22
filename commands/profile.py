"""Komenda /profile łącząca zapisane karty AOTY i Last.fm."""

from __future__ import annotations

import asyncio

import discord
import requests

import aoty
from display_utils import display_release_date, display_romanized_name
from lastfm_archive import LASTFM_ARCHIVE
from lastfm_database import LASTFM_DB
from settings import (
    KOTONE_USERS,
    KOTONE_USERS_BY_AOTY,
    resolve_aoty_username,
    resolve_kotone_profile,
)
from shared import (
    build_profile_variables,
    rating_flags_text,
    score_color,
    score_or_nr,
    set_aoty_footer,
    username_autocomplete,
    application_avatar_emoji,
)
from services import DATA
from ui_constants import (
    LASTFM_ICON,
    LASTFM_ICON_ATTACHMENT,
    LASTFM_ICON_FILENAME,
    SOURCE_EMOJIS,
)
from views import ProfilePagerView


def _cached_release_url(item: dict) -> str:
    """Return a durable AOTY release URL, even for older imported rows.

    Historical CSV/manual rows may have the AOTY album ID before a scraper
    captured its canonical slug.  The ID-only route is valid on AOTY and is
    preferable to rendering a broken Markdown link such as ``(None)``.
    """

    url = str(item.get("url") or item.get("album_url") or "").strip()
    if url and url.casefold() not in {"none", "null", "—"}:
        return url

    album_id = str(item.get("album_id") or "").strip()
    if album_id.isdigit():
        return f"{aoty.BASE_URL}/album/{album_id}/"
    return ""


def _linked_release_text(artist: str, album: str, item: dict) -> str:
    """Render a release with a link only when a valid URL is available."""

    label = f"{artist} — {album}"
    url = _cached_release_url(item)
    return f"[{label}]({url})" if url else label


def _favorite_line(item: dict) -> str:
    url = _cached_release_url(item)
    item_type = item.get("type")

    if item_type == "artist":
        name = display_romanized_name(item.get("name") or "Nieznany artysta")
        return f"\⭐ **[{name}]({url})**" if url else f"\⭐ **{name}**"

    album = display_romanized_name(
        item.get("album") or item.get("name") or "Nieznane wydanie"
    )
    artist = item.get("artist")
    display_artist = display_romanized_name(artist) if artist else None

    if display_artist:
        return f"\💿 **{_linked_release_text(display_artist, album, item)}**"

    return f"\💿 **[{album}]({url})**" if url else f"\💿 **{album}**"


def _recent_line(item: dict) -> str:
    artist = display_romanized_name(item.get("artist") or "Nieznany artysta")
    album = display_romanized_name(item.get("album") or "Nieznane wydanie")
    score = item.get("score") or "NR"
    release_format = item.get("release_format") or "—"
    rating_date = display_release_date(item.get("rating_date"))
    flags = rating_flags_text(item)
    flags_text = f" · {flags}" if flags else ""
    date_text = f" • {rating_date}" if rating_date != "—" else ""

    release = _linked_release_text(artist, album, item)
    return f"{score_or_nr(score)}  {release}  •  {release_format}  •  {date_text}{flags_text}"


def _lastfm_count(value: object) -> str:
    try:
        return f"{int(value):,}".replace(",", " ")
    except (TypeError, ValueError):
        return "—"


def _lastfm_timestamp(value: object) -> str:
    """Render the archived Unix scrobble time as Discord timestamps."""

    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return ""
    return f"\n<t:{timestamp}:F>  •  <t:{timestamp}:R>"


def _lastfm_avatar_url(profile: dict[str, object]) -> str | None:
    """Return the current Last.fm avatar with a DB-versioned cache key.

    Discord caches remote images very aggressively.  Last.fm may keep the
    same avatar address while changing it from a still PNG to an animated GIF,
    so the timestamp saved with the Last.fm profile is appended solely to make
    Discord fetch the current asset immediately after a profile refresh.
    """

    url = str(profile.get("avatar_url") or "").strip()
    if not url:
        return None
    try:
        version = int(float(profile.get("fetched_at") or 0))
    except (TypeError, ValueError):
        version = 0
    if version:
        separator = "&" if "?" in url else "?"
        return f"{url}{separator}kotone_avatar={version}"
    return url


def _set_lastfm_footer(embed: discord.Embed) -> None:
    """Apply the bundled Last.fm icon to every Last.fm profile card."""

    embed.set_footer(
        text="Last.fm  •  dane zapisane w kotone",
        icon_url=LASTFM_ICON_ATTACHMENT,
    )


def _lastfm_footer_file() -> discord.File:
    """Return a fresh attachment; Discord files cannot be reused between sends."""

    return discord.File(LASTFM_ICON, filename=LASTFM_ICON_FILENAME)


def _lastfm_archive_progress(profile_key: object, archive: dict | None = None) -> str:
    """Explain archive progress without comparing Last.fm library counters.

    The API's artist/album counts are library counters, while Kotone counts
    distinct names in scrobbles.  Only the total scrobble count is comparable.
    """

    progress = LASTFM_DB.archive_progress(profile_key)
    saved = int((archive or progress).get("scrobbles") or 0)
    total = progress.get("total_scrobbles")
    if total:
        base = f"**{_lastfm_count(saved)} / {_lastfm_count(total)}** scrobbli"
    else:
        base = f"**{_lastfm_count(saved)}** scrobbli"
    # A completed cursor only means that a previous crawl reached its last
    # page.  Do not call the archive complete if the current Last.fm total is
    # greater than the number of rows Kotone actually has saved.
    if progress.get("complete") and (not total or saved >= int(total)):
        return f"{base} • archiwum kompletne"
    total_pages = progress.get("total_pages")
    next_page = progress.get("next_page")
    if total_pages:
        return f"{base} • import w tle: strona {next_page}/{total_pages}"
    return f"{base} • oczekuje na pierwszy import"


def _build_lastfm_embed(
    kotone_profile: dict[str, object],
    *,
    author_name: str | None = None,
    author_url: str | None = None,
    author_icon_url: str | None = None,
) -> discord.Embed:
    """Build a Last.fm card, mirroring the AOTY profile author when present."""

    profile_key = str(kotone_profile.get("name") or "").strip()
    lastfm_username = str(kotone_profile.get("lastfm_username") or "").strip()
    lastfm_profile = LASTFM_DB.get_profile(profile_key) or {}
    archive = LASTFM_DB.archive_statistics(profile_key)
    latest = LASTFM_DB.latest_scrobble(profile_key)
    profile_url = str(
        lastfm_profile.get("profile_url")
        or f"https://www.last.fm/user/{lastfm_username}"
    )
    avatar = _lastfm_avatar_url(lastfm_profile)
    display_name = str(author_name or kotone_profile.get("name") or profile_key)
    embed = discord.Embed(
        title=f"{SOURCE_EMOJIS['lastfm']} {lastfm_username}",
        url=profile_url,
        color=discord.Color.from_rgb(206, 69, 69),
    )
    embed.add_field(
        name=f" ",
        value=(
            f"**{_lastfm_count(lastfm_profile.get('total_scrobbles'))}** scrobbli  •  "
            f"**{_lastfm_count(lastfm_profile.get('artist_count'))}** wykonawców\n"
            f"**{_lastfm_count(lastfm_profile.get('album_count'))}** albumów  •  "
            f"**{_lastfm_count(lastfm_profile.get('track_count'))}** utworów"
        ),
        inline=False,
    )
    embed.add_field(
        name=application_avatar_emoji(),
        value=(
            f"{_lastfm_archive_progress(profile_key, archive)}\n"
            f"**{_lastfm_count(archive['artists'])}** wykonawców  •  "
            f"**{_lastfm_count(archive['albums'])}** albumów  •  "
            f"**{_lastfm_count(archive['tracks'])}** utworów"
        ),
        inline=False,
    )
    if latest:
        album = f" — {latest['album']}" if latest.get("album") else ""
        embed.add_field(
            name="Ostatni scrobble",
            value=(
                f"**{latest.get('artist') or '—'} — "
                f"{latest.get('track') or '—'}**{album}"
                f"{_lastfm_timestamp(latest.get('played_at'))}"
            ),
            inline=False,
    )
    # This is a Last.fm card, so its author always links to and uses the
    # Last.fm profile rather than the corresponding AOTY account.
    source_avatar = avatar
    if source_avatar:
        embed.set_author(
            name=display_name,
            url=profile_url,
            icon_url=source_avatar,
        )
    else:
        embed.set_author(name=display_name, url=profile_url)
    if avatar:
        embed.set_thumbnail(url=avatar)
    _set_lastfm_footer(embed)
    return embed


async def profile_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[discord.app_commands.Choice[str]]:
    """Offer Kotone profiles, including Last.fm-only Gan, before AOTY names."""

    needle = str(current or "").strip().casefold()
    choices: list[discord.app_commands.Choice[str]] = []
    seen: set[str] = set()
    for key, profile in KOTONE_USERS.items():
        label = str(profile.get("name") or key)
        aliases = " ".join(
            [
                key,
                label,
                str(profile.get("aoty_username") or ""),
                str(profile.get("lastfm_username") or ""),
            ]
        ).casefold()
        if needle and needle not in aliases:
            continue
        value = str(profile.get("aoty_username") or label)
        if value.casefold() not in seen:
            suffix = "Last.fm" if not profile.get("aoty_username") else "Kotone"
            choices.append(discord.app_commands.Choice(name=f"{label} · {suffix}", value=value))
            seen.add(value.casefold())
    for choice in await username_autocomplete(interaction, current):
        if choice.value.casefold() not in seen:
            choices.append(choice)
            seen.add(choice.value.casefold())
    return choices[:10]


def setup_profile_command(tree: discord.app_commands.CommandTree):
    @tree.command(
        name="profile",
        description="Pokazuje profil AOTY albo Last.fm użytkownika kotone.",
    )
    @discord.app_commands.describe(username="Użytkownik AOTY lub kotone")
    @discord.app_commands.autocomplete(username=profile_autocomplete)
    async def profile_command(
        interaction: discord.Interaction,
        username: str | None = None,
    ):
        await interaction.response.defer()
        kotone_profile = resolve_kotone_profile(interaction.user.id, username)
        if kotone_profile and kotone_profile.get("lastfm_username"):
            # Refresh only the newest Last.fm page for this profile.  This
            # never resumes the imported newest-to-oldest archive. The small
            # profile response also stores the current Last.fm avatar URL.
            await LASTFM_ARCHIVE.refresh_latest_scrobble(
                kotone_profile.get("name") or username,
                refresh_profile=True,
            )
        if kotone_profile and not kotone_profile.get("aoty_username"):
            await interaction.followup.send(
                embed=_build_lastfm_embed(kotone_profile),
                file=_lastfm_footer_file(),
            )
            return

        username = resolve_aoty_username(interaction.user.id, username)
        if not username:
            await interaction.followup.send(
                "❌ Wpisz `username` albo wywołaj tę komendę z konta "
                "użytkownika Kotone w `config.json`.",
                ephemeral=True,
            )
            return

        try:
            # Command rendering is always SQLite-only. AOTY updates happen
            # exclusively through the monitor/background worker.
            profile = await DATA.get_profile(
                username,
                recent_limit=50,
                allow_network=False,
            )
        except ValueError as exc:
            await interaction.followup.send(f"ℹ️ {exc}")
            return
        except aoty.AOTYUserNotFound:
            await interaction.followup.send(
                f"❌ Konto AOTY **{username}** nie istnieje."
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

        variables = build_profile_variables(profile, username)

        # Te same nazwy co wcześniej pozostają czytelne w embedzie; ich
        # defaulty/źródło są jednak centralnie zdefiniowane w shared.py.
        display_username = variables.display_username
        avatar = variables.avatar
        profile_url = variables.profile_url
        ratings_count = variables.ratings_count
        reviews_count = variables.reviews_count
        lists_count = variables.lists_count
        following_count = variables.following_count
        followers_count = variables.followers_count
        # The saved archive is the authoritative number for configured users.
        # AOTY only exposes a distribution-based approximation; never show it
        # in place of the exact SQLite average.
        sqlite_average = profile.get("sqlite_average_rating")
        sqlite_average_count = int(profile.get("sqlite_average_count") or 0)
        average_rating = (
            float(sqlite_average)
            if sqlite_average is not None
            else variables.average_rating
        )
        average_rating_text = (
            f"{average_rating:.1f}"
            if average_rating is not None
            else "Brak danych"
        )
        favorites = variables.favorites[:5]
        favorite_kind = variables.favorite_kind
        if favorite_kind == "artists":
            favorites_field_name = "❤️ Favorite Artists"
        elif favorite_kind == "albums":
            favorites_field_name = "❤️ Favorite Albums"
        else:
            favorites_field_name = "Favorites"

        favorite_lines = [_favorite_line(item) for item in favorites]
        recent_ratings = variables.recent_ratings[:50]
        total_pages = max(1, min(10, (len(recent_ratings) + 4) // 5))

        embed_color = score_color(round(average_rating)) if average_rating is not None else None

        def build_page_embed(page_index: int) -> discord.Embed:
            page_index = max(0, min(page_index, total_pages - 1))
            start = page_index * 5
            page_items = recent_ratings[start:start + 5]
            recent_lines = [_recent_line(item) for item in page_items]

            embed = discord.Embed(
                title=" ",
                url=profile_url,
                description=(
                    f"\⭐ **{ratings_count}**  ⌀ **{average_rating_text}**  
                    ✎ **{reviews_count}**  **⫶☰ {lists_count}**"
                ),
                color=embed_color,
            )

            embed.add_field(
                name=" ",
                value=(
                    f"Following: **{following_count}**\n"
                    f"Followers: **{followers_count}**"
                ),
                inline=False,
            )

            if avatar:
                embed.set_author(
                    name=display_username,
                    url=profile_url,
                    icon_url=avatar,
                )
                embed.set_thumbnail(url=avatar)
            else:
                embed.set_author(
                    name=display_username,
                    url=profile_url,
                )

            # Zgodnie z ustawieniem profilu AOTY pokazujemy tylko ten typ
            # Favorites, który user ma wybrany jako domyślny.
            embed.add_field(
                name=favorites_field_name,
                value="\n".join(favorite_lines) if favorite_lines else "—",
                inline=False,
            )

            embed.add_field(
                name=f"\⭐ Ostatnie 5 ocen  •  [{page_index + 1}/{total_pages}]",
                value="\n".join(recent_lines) if recent_lines else "—",
                inline=False,
            )

            set_aoty_footer(
                embed,
                (
                    f"AOTY.org  •  średnia wyliczona przez kotone"
                    if sqlite_average is not None
                    else "AOTY.org  •  średnia jest przybliżona z Rating Distribution"
                ),
            )
            return embed

        # A supplementary Last.fm card is available only to Kotone users who
        # have both configured accounts.  The card mirrors the visible AOTY
        # author so both sources clearly belong to the same person.
        kotone_profile = KOTONE_USERS_BY_AOTY.get(username.casefold())
        lastfm_embed = (
            _build_lastfm_embed(
                kotone_profile,
                author_name=display_username,
                author_url=profile_url,
                author_icon_url=avatar,
            )
            if kotone_profile and kotone_profile.get("lastfm_username")
            else None
        )

        view = ProfilePagerView(
            username=username,
            ratings=recent_ratings,
            favorites=variables.favorites,
            build_page_embed=build_page_embed,
            supplemental_embeds=[lastfm_embed] if lastfm_embed else [],
            owner_id=interaction.user.id,
        )

        send_kwargs = {
            "embeds": view.build_message_embeds(0),
            "view": view,
            "wait": True,
        }
        if lastfm_embed:
            send_kwargs["file"] = _lastfm_footer_file()
        message = await interaction.followup.send(**send_kwargs)
        view.bind_message(message)
"""Komenda /profile łącząca zapisane karty AOTY i Last.fm."""
