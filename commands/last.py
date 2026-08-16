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

def setup_last_command(
    tree,
    get_ratings,
    get_user_avatar,
    get_album_details,
    aoty_user_exists,
    score_color,
    score_icon,
    AOTYRateLimit,
):

    @tree.command(
        name="last",
        description="Pokazuje ostatnią ocenę użytkownika AOTY"
    )
    @discord.app_commands.describe(
        username="Nazwa użytkownika na AOTY"
    )

    async def ostatnia(
        interaction: discord.Interaction,
        username: str
    ):

        await interaction.response.defer()

        username = username.strip()

        try:
            exists = await asyncio.to_thread(
                aoty_user_exists,
                username
            )

            if not exists:
                await interaction.followup.send(
                    f"❌ Konto AOTY **{username}** nie istnieje."
                )
                return

            ratings = await asyncio.to_thread(
                get_ratings,
                username
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

        if not ratings:
            await interaction.followup.send(
                f"❌ Nie znaleziono ocen użytkownika **{username}**."
            )
            return

        avatar = None
        try:
            avatar = await asyncio.to_thread(
                get_user_avatar,
                username
            )
        except Exception:
            pass

        latest = ratings[0]

        score = latest["score"]
        artist = latest["artist"]
        album = latest["album"]

        display_artist = display_romanized_name(artist)
        display_album = display_romanized_name(album)

        date = latest["date"]
        url = latest["url"]
        cover = latest["cover"]

        # Dodatkowe dane albumu do użycia w embedzie.
        user_score = "?"
        aoty_user_score = "?"
        ratings_count = "?"

        release_date = "?"
        year = " "
        album_format = "?"

        label = "?"
        labels = []
        labels_text = "?"

        genres = []
        genres_text = "?"
        main_genre = "?"
        other_genres = "?"
        other_genres_text = "?"
        all_genres_text = "?"

        secondary_genres = []
        secondary_genres_text = "?"

        vibes = []
        vibes_text = "?"

        ranking_year = "?"
        year_ranking = "?"
        year_ranking_text = "?"

        tracklist = []
        tracklist_text = "?"

        try:
            details = await asyncio.to_thread(
                get_album_details,
                url
            )

            user_score = details.get("user_score") or "?"
            aoty_user_score = user_score
            ratings_count = details.get("ratings_count") or "?"

            release_date = details.get("release_date") or "?"
            year = details.get("year") or "?"
            album_format = details.get("album_format") or "?"

            label = details.get("label") or "?"
            labels = details.get("labels") or []
            labels_text = details.get("labels_text") or "?"

            genres = details.get("genres") or []
            genres_text = details.get("genres_text") or " "

            secondary_genres = details.get("secondary_genres") or []
            secondary_genres_text = (
                details.get("secondary_genres_text")
                or " "
            )

            vibes = details.get("vibes") or []
            vibes_text = details.get("vibes_text") or " "

            ranking_year = details.get("ranking_year") or "?"
            year_ranking = details.get("year_ranking") or "?"
            year_ranking_text = (
                details.get("year_ranking_text")
                or "?"
            )

            tracklist = details.get("tracklist") or []
            tracklist_text = details.get("tracklist_text") or "?"

            if genres:
                main_genre = genres[0]

                if len(genres) > 1:
                    other_genres = ", ".join(genres[1:])
                    other_genres_text = other_genres
                    all_genres_text = f"**{main_genre}**, {other_genres_text}"
                else:
                    all_genres_text = f"**{main_genre}**"

        except Exception:
            pass

        file = discord.File(AOTY_ICON, filename="aoty.jpg")

        embed = discord.Embed(
                title=f"\{score_icon(score)} {display_artist} — **{display_album}** ({year})",
                url=url,
                description=f"{all_genres_text}\n{secondary_genres_text}\n{vibes_text}\n# \⭐ **{score}** \⭐",
                color=score_color(score),
        )
        
        embed.add_field(
                name=f"\👥 **{aoty_user_score}**/{ratings_count}",
                value=" ",
                inline=True

        )
        embed.add_field(
                name=f"\📅 **{year_ranking_text}**",
                value=" ",
                inline=True
        )
        
        if avatar:
            embed.set_author(
                name=f"{username}  •  {date}",
                url=f"https://www.albumoftheyear.org/user/{username}",
                icon_url=avatar
            )
        else:
            embed.set_author(
                name=f"{username}  •  {date}",
            )

        if cover:
            embed.set_thumbnail(
                url=cover
            )

        embed.set_footer(
            text=f"{album_format}  •  {release_date}  •  {label}",
            icon_url="attachment://aoty.jpg"
        )

        await interaction.followup.send(
            embed=embed,
            file=file
        )