import asyncio

import discord
import requests

import aoty
from services import DATA
from lastfm_database import LASTFM_DB
from settings import (
    KOTONE_USERS,
    LASTFM_ICON,
    LASTFM_ICON_ATTACHMENT,
    LASTFM_ICON_FILENAME,
    SOURCE_EMOJIS,
    resolve_aoty_username,
    resolve_kotone_profile,
)
from display_utils import display_romanized_name
from shared import (
    build_profile_variables,
    rating_flags_text,
    score_color,
    score_or_nr,
    set_aoty_footer,
    username_autocomplete,
)
from views import ProfilePagerView


def _favorite_line(item: dict) -> str:
    url = item.get("url")
    item_type = item.get("type")

    if item_type == "artist":
        name = display_romanized_name(item.get("name") or "Nieznany artysta")
        return f"\⭐ **[{name}]({url})**"

    album = display_romanized_name(
        item.get("album") or item.get("name") or "Nieznane wydanie"
    )
    artist = item.get("artist")
    display_artist = display_romanized_name(artist) if artist else None

    if display_artist:
        return f"\💿 **[{display_artist} — {album}]({url})**"

    return f"\💿 **[{album}]({url})**"


def _recent_line(item: dict) -> str:
    artist = display_romanized_name(item.get("artist") or "Nieznany artysta")
    album = display_romanized_name(item.get("album") or "Nieznane wydanie")
    score = item.get("score") or "NR"
    url = item.get("url")
    release_format = item.get("release_format") or "—"
    flags = rating_flags_text(item)
    flags_text = f" · {flags}" if flags else ""

    return f"{score_or_nr(score)} · [{artist} — {album}]({url}) · {release_format}{flags_text}"


def _lastfm_count(value: object) -> str:
    try:
        return f"{int(value):,}".replace(",", " ")
    except (TypeError, ValueError):
        return "—"


def _set_lastfm_footer(embed: discord.Embed) -> None:
    """Apply the bundled Last.fm icon to every Last.fm profile card."""

    embed.set_footer(
        text="Last.fm • dane zapisane przez Kotone",
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
    if progress.get("complete"):
        return f"{base} • archiwum kompletne"
    total_pages = progress.get("total_pages")
    next_page = progress.get("next_page")
    if total_pages:
        return f"{base} • import w tle: strona {next_page}/{total_pages}"
    return f"{base} • oczekuje na pierwszy import"


def _build_lastfm_only_embed(kotone_profile: dict[str, object]) -> discord.Embed:
    """Build the sole profile card for a Kotone user without AOTY."""

    profile_key = str(kotone_profile.get("name") or "").strip()
    lastfm_username = str(kotone_profile.get("lastfm_username") or "").strip()
    lastfm_profile = LASTFM_DB.get_profile(profile_key) or {}
    archive = LASTFM_DB.archive_statistics(profile_key)
    latest = LASTFM_DB.latest_scrobble(profile_key)
    profile_url = str(
        lastfm_profile.get("profile_url")
        or f"https://www.last.fm/user/{lastfm_username}"
    )
    avatar = str(lastfm_profile.get("avatar_url") or "").strip() or None
    display_name = str(kotone_profile.get("name") or profile_key)
    embed = discord.Embed(
        title=f"{SOURCE_EMOJIS['lastfm']} Last.fm — @{lastfm_username}",
        url=profile_url,
        color=discord.Color.from_rgb(206, 69, 69),
    )
    embed.add_field(
        name="Konto Last.fm",
        value=(
            f"**{_lastfm_count(lastfm_profile.get('total_scrobbles'))}** scrobbli  •  "
            f"**{_lastfm_count(lastfm_profile.get('artist_count'))}** wykonawców\n"
            f"**{_lastfm_count(lastfm_profile.get('album_count'))}** albumów  •  "
            f"**{_lastfm_count(lastfm_profile.get('track_count'))}** utworów"
        ),
        inline=False,
    )
    embed.add_field(
        name="Historia odsłuchów w Kotone",
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
            ),
            inline=False,
        )
    if avatar:
        embed.set_author(name=display_name, url=profile_url, icon_url=avatar)
        embed.set_thumbnail(url=avatar)
    else:
        embed.set_author(name=display_name, url=profile_url)
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
        description="Pokazuje profil AOTY albo Last.fm użytkownika Kotone.",
    )
    @discord.app_commands.describe(username="Użytkownik AOTY lub Kotone")
    @discord.app_commands.autocomplete(username=profile_autocomplete)
    async def profile_command(
        interaction: discord.Interaction,
        username: str | None = None,
    ):
        await interaction.response.defer()
        kotone_profile = resolve_kotone_profile(interaction.user.id, username)
        if kotone_profile and not kotone_profile.get("aoty_username"):
            await interaction.followup.send(
                embed=_build_lastfm_only_embed(kotone_profile),
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
            favorites_field_name = "Favorite Artists"
        elif favorite_kind == "albums":
            favorites_field_name = "Favorite Albums"
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
                title=display_username,
                url=profile_url,
                description=(
                    f"**{ratings_count}** ocen  •  "
                    f"x̄ **{average_rating_text}**"
                ),
                color=embed_color,
            )

            embed.add_field(
                name=" ",
                value=(
                    f"Reviews **{reviews_count}**  •  "
                    f"Lists **{lists_count}**\n"
                    f"Following **{following_count}**  •  "
                    f"Followers **{followers_count}**"
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
                name=f"Ostatnie 5 ocen [{page_index + 1}/{total_pages}]",
                value="\n".join(recent_lines) if recent_lines else "—",
                inline=False,
            )

            set_aoty_footer(
                embed,
                (
                    f"SQLite • średnia z {sqlite_average_count} zapisanych ocen"
                    if sqlite_average is not None
                    else "AOTY.org • średnia jest przybliżona z Rating Distribution"
                ),
            )
            return embed

        view = ProfilePagerView(
            username=username,
            ratings=recent_ratings,
            favorites=variables.favorites,
            build_page_embed=build_page_embed,
            owner_id=interaction.user.id,
        )

        send_kwargs = {
            "embeds": view.build_message_embeds(0),
            "view": view,
            "wait": True,
        }
        message = await interaction.followup.send(**send_kwargs)
        view.bind_message(message)
