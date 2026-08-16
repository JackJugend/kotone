import asyncio
import discord
import requests

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
        date = latest["date"]
        url = latest["url"]
        cover = latest["cover"]

        # Dodatkowe dane albumu do użycia w embedzie.
        year = " "
        genres = []
        genres_text = "Brak danych"
        main_genre = "Brak danych"
        other_genres = "Brak danych"
        other_genres_text = "Brak danych"
        all_genres_text = "Brak danych"

        try:
            details = await asyncio.to_thread(
                get_album_details,
                url
            )

            year = details.get("year") or "Brak danych"
            genres = details.get("genres") or []
            genres_text = details.get("genres_text") or "Brak danych"

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

        embed = discord.Embed(
                title=f"{artist} - **{album}** ({year})",
                url=url,
                description=f"{all_genres_text}",
                color=score_color(score),
        )

        embed.add_field(
                name=f"\⭐ **{score}** {score_icon(score)}",
                value=" ",
                inline=False
        )
        
        if avatar:
            embed.set_author(
                name=username,
                url=f"https://www.albumoftheyear.org/user/{username}",
                icon_url=avatar
            )
        else:
            embed.set_author(
                name=username
            )

        if cover:
            embed.set_thumbnail(
                url=cover
            )

        embed.set_footer(
            text=f"• {date}",
            icon_url="https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcSiJt1MSjldtmrIaTGoE2r3CgsaPB8l1UneW-j9w103bSS5ft45C-OLTCg&s=10"
        )

        await interaction.followup.send(
            embed=embed
        )