import asyncio

import discord
import requests

from display_utils import display_romanized_name

import os

BASE_DIR = os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)

AOTY_ICON = os.path.join(
    BASE_DIR,
    "assets",
    "aoty.jpg"
)


def _favorite_line(item):

    url = item.get("url")
    item_type = item.get("type")

    if item_type == "artist":
        name = item.get("name") or "Nieznany artysta"
        display_name = display_romanized_name(name)
        return f"\⭐ **[{display_name}]({url})**"

    album = item.get("album") or item.get("name") or "Nieznane wydanie"
    artist = item.get("artist")

    display_album = display_romanized_name(album)
    display_artist = (
        display_romanized_name(artist)
        if artist
        else None
    )

    if display_artist:
        return f"\💿 **[{display_artist} — {display_album}]({url})**"

    return f"\💿 **[{display_album}]({url})**"


def _recent_line(item, score_icon):

    artist = item.get("artist") or "Nieznany artysta"
    album = item.get("album") or "Nieznane wydanie"

    display_artist = display_romanized_name(artist)
    display_album = display_romanized_name(album)

    score = item.get("score") or "NR"
    url = item.get("url")
    release_format = item.get("release_format") or "?"

    return (
        f"\{score_icon(score)} **{score}** · "
        f"[{display_artist} — {display_album}]({url}) · {release_format}"
    )


def setup_profile_command(
    tree,
    get_profile_data,
    aoty_user_exists,
    score_color,
    score_icon,
    AOTYRateLimit,
):
    @tree.command(
        name="profile",
        description="Pokazuje profil użytkownika AOTY",
    )
    @discord.app_commands.describe(
        username="Nazwa użytkownika na AOTY",
    )
    async def profile_command(
        interaction: discord.Interaction,
        username: str,
    ):
        await interaction.response.defer()
        username = username.strip()

        try:
            exists = await asyncio.to_thread(
                aoty_user_exists,
                username,
            )

            if not exists:
                await interaction.followup.send(
                    f"❌ Konto AOTY **{username}** nie istnieje."
                )
                return

            profile = await asyncio.to_thread(
                get_profile_data,
                username,
            )

        except AOTYRateLimit:
            await interaction.followup.send(
                "⚠️ AOTY chwilowo ogranicza liczbę zapytań."
            )
            return

        except requests.RequestException as e:
            await interaction.followup.send(
                f"❌ Błąd połączenia z AOTY: `{e}`"
            )
            return

        except Exception as e:
            await interaction.followup.send(
                f"❌ Błąd: `{type(e).__name__}: {e}`"
            )
            return

        display_username = profile.get("username") or username
        avatar = profile.get("avatar")
        profile_url = profile.get("url")

        ratings_count = profile.get("ratings_count") or "0"
        reviews_count = profile.get("reviews_count") or "0"
        lists_count = profile.get("lists_count") or "0"
        following_count = profile.get("following_count") or "0"
        followers_count = profile.get("followers_count") or "0"
        average_rating_text = (
            profile.get("average_rating_text")
            or "Brak danych"
        )

        favorite_albums = (
            profile.get("favorite_albums")
            or []
        )

        favorite_artists = (
            profile.get("favorite_artists")
            or []
        )

        # Fallback dla starszej wersji get_profile_data.
        if not favorite_albums and not favorite_artists:
            legacy_favorites = (
                profile.get("favorites")
                or []
            )

            favorite_albums = [
                item
                for item in legacy_favorites
                if item.get("type") == "album"
            ][:5]

            favorite_artists = [
                item
                for item in legacy_favorites
                if item.get("type") == "artist"
            ][:5]

        recent_ratings = profile.get("recent_ratings") or []

        favorite_album_lines = [
            _favorite_line(item)
            for item in favorite_albums[:5]
        ]

        favorite_artist_lines = [
            _favorite_line(item)
            for item in favorite_artists[:5]
        ]

        recent_lines = [
            _recent_line(item, score_icon)
            for item in recent_ratings[:5]
        ]

        average_rating = profile.get("average_rating")

        embed_color = None

        if average_rating is not None:
            embed_color = score_color(
                round(average_rating)
            )

        file = discord.File(AOTY_ICON, filename="aoty.jpg")

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
            embed.set_thumbnail(
                url=avatar,
            )
        else:
            embed.set_author(
                name=display_username,
                url=profile_url,
            )

        embed.add_field(
            name="Favorite Albums",
            value=(
                "\n".join(favorite_album_lines)
                if favorite_album_lines
                else "—"
            ),
            inline=False,
        )

        embed.add_field(
            name="Favorite Artists",
            value=(
                "\n".join(favorite_artist_lines)
                if favorite_artist_lines
                else "—"
            ),
            inline=False,
        )

        embed.add_field(
            name="Ostatnie 5 ocen",
            value=(
                "\n".join(recent_lines)
                if recent_lines
                else "—"
            ),
            inline=False,
        )

        embed.set_footer(
            text="AOTY.org • średnia jest przybliżona z Rating Distribution",
            icon_url="attachment://aoty.jpg"
        )

        await interaction.followup.send(
            embed=embed,
            file=file
        )
