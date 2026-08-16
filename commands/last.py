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
        user_score = "Brak danych"
        aoty_user_score = "Brak danych"
        ratings_count = "Brak danych"

        release_date = "Brak danych"
        year = " "
        album_format = "Brak danych"

        label = "Brak danych"
        labels = []
        labels_text = "Brak danych"

        genres = []
        genres_text = "Brak danych"
        main_genre = "Brak danych"
        other_genres = "Brak danych"
        other_genres_text = "Brak danych"
        all_genres_text = "Brak danych"

        secondary_genres = []
        secondary_genres_text = "Brak danych"

        vibes = []
        vibes_text = "Brak danych"

        ranking_year = "Brak danych"
        year_ranking = "Brak danych"
        year_ranking_text = "Brak danych"

        tracklist = []
        tracklist_text = "Brak danych"

        try:
            details = await asyncio.to_thread(
                get_album_details,
                url
            )

            user_score = details.get("user_score") or "Brak danych"
            aoty_user_score = user_score
            ratings_count = details.get("ratings_count") or "Brak danych"

            release_date = details.get("release_date") or "Brak danych"
            year = details.get("year") or "Brak danych"
            album_format = details.get("album_format") or "Brak danych"

            label = details.get("label") or "Brak danych"
            labels = details.get("labels") or []
            labels_text = details.get("labels_text") or "Brak danych"

            genres = details.get("genres") or []
            genres_text = details.get("genres_text") or "Brak danych"

            secondary_genres = details.get("secondary_genres") or []
            secondary_genres_text = (
                details.get("secondary_genres_text")
                or "Brak danych"
            )

            vibes = details.get("vibes") or []
            vibes_text = details.get("vibes_text") or "Brak danych"

            ranking_year = details.get("ranking_year") or "Brak danych"
            year_ranking = details.get("year_ranking") or "Brak danych"
            year_ranking_text = (
                details.get("year_ranking_text")
                or "Brak danych"
            )

            tracklist = details.get("tracklist") or []
            tracklist_text = details.get("tracklist_text") or "Brak danych"

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
                title=f"{score_icon(score)}\uFE0E {artist} • **{album}** ({year})",
                url=url,
                description=f"{all_genres_text}",
                color=score_color(score),
        )

        embed.add_field(
                name=f"⭐\uFE0E **{score}**",
                value=" ",
                inline=True
        
        )
                embed.add_field(
                name=f"👥\uFE0E **{aoty_user_score}**/{ratings_count}",
                value=" ",
                inline=True

        )
                embed.add_field(
                name=f"📅\uFE0E **{year_ranking_text}**",
                value=" ",
                inline=True
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
            text=f"•  {date}",
            icon_url="https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcSiJt1MSjldtmrIaTGoE2r3CgsaPB8l1UneW-j9w103bSS5ft45C-OLTCg&s=10"
        )

        await interaction.followup.send(
            embed=embed
        )