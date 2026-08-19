import asyncio

import discord
import requests

import aoty
from services import DATA
from lastfm_database import LASTFM_DB
from settings import KOTONE_USERS_BY_AOTY, SOURCE_EMOJIS, resolve_aoty_username
from display_utils import display_romanized_name
from shared import (
    build_profile_variables,
    rating_flags_text,
    score_color,
    score_icon,
    set_aoty_footer,
    user_avatar_emoji,
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

    return (
        f"{score_icon(score)} **{score}** · "
        f"[{artist} — {album}]({url}) · {release_format}{flags_text}"
    )


def _lastfm_count(value: object) -> str:
    try:
        return f"{int(value):,}".replace(",", " ")
    except (TypeError, ValueError):
        return "—"


def setup_profile_command(tree: discord.app_commands.CommandTree):
    @tree.command(
        name="profile",
        description="Pokazuje profil użytkownika AOTY.",
    )
    @discord.app_commands.describe(username="Użytkownik AOTY")
    @discord.app_commands.autocomplete(username=username_autocomplete)
    async def profile_command(
        interaction: discord.Interaction,
        username: str | None = None,
    ):
        await interaction.response.defer()
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
        kotone_profile = KOTONE_USERS_BY_AOTY.get(username.casefold())
        lastfm_profile = (
            LASTFM_DB.get_profile((kotone_profile or {}).get("name"))
            if kotone_profile and kotone_profile.get("lastfm_username")
            else None
        )
        last_scrobble = (
            LASTFM_DB.latest_scrobble((kotone_profile or {}).get("name"))
            if kotone_profile and kotone_profile.get("lastfm_username")
            else None
        )
        avatar_emoji = user_avatar_emoji(username)

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
                    name=f"{avatar_emoji} {display_username}".strip(),
                    url=profile_url,
                    icon_url=avatar,
                )
                embed.set_thumbnail(url=avatar)
            else:
                embed.set_author(
                    name=f"{avatar_emoji} {display_username}".strip(),
                    url=profile_url,
                )

            if lastfm_profile:
                lines = [
                    f"{SOURCE_EMOJIS['lastfm']} **Last.fm · @{lastfm_profile['lastfm_username']}**",
                    f"🎧 {_lastfm_count(lastfm_profile.get('total_scrobbles'))} scrobbli"
                    f" • { _lastfm_count(lastfm_profile.get('artist_count')) } wykonawców"
                    f" • { _lastfm_count(lastfm_profile.get('album_count')) } albumów",
                ]
                if last_scrobble:
                    album_text = f" — {last_scrobble['album']}" if last_scrobble.get('album') else ""
                    lines.append(
                        f"Ostatni scrobble: **{last_scrobble['artist']} — "
                        f"{last_scrobble['track']}**{album_text}"
                    )
                embed.add_field(
                    name=f"{avatar_emoji} Dane odsłuchów".strip(),
                    value="\n".join(lines),
                    inline=False,
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

        first_embed = build_page_embed(0)
        view = ProfilePagerView(
            username=username,
            ratings=recent_ratings,
            favorites=variables.favorites,
            build_page_embed=build_page_embed,
            owner_id=interaction.user.id,
        )

        message = await interaction.followup.send(
            embed=first_embed,
            view=view,
            wait=True,
        )
        view.bind_message(message)
