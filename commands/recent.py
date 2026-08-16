import asyncio
import discord
import requests


def _rating_embed(
    username,
    item,
    avatar,
    details,
    score_color,
    score_icon,
):
    score = item["score"]
    artist = item["artist"]
    album = item["album"]
    date = item["date"]
    url = item["url"]
    cover = item["cover"]

    year = details.get("year") or "Brak danych"
    genres = details.get("genres") or []

    if genres:
        main_genre = genres[0]
        other_genres_text = ", ".join(genres[1:])

        if other_genres_text:
            all_genres_text = f"**{main_genre}**, {other_genres_text}"
        else:
            all_genres_text = f"**{main_genre}**"
    else:
        main_genre = "Brak danych"
        other_genres_text = "Brak danych"
        all_genres_text = "Brak danych"

    # Zmienne szczegółów zostają dostępne tutaj tak samo jak w /last.
    user_score = details.get("user_score") or "Brak danych"
    aoty_user_score = user_score
    ratings_count = details.get("ratings_count") or "Brak danych"
    release_date = details.get("release_date") or "Brak danych"
    album_format = details.get("album_format") or "Brak danych"
    label = details.get("label") or "Brak danych"
    labels = details.get("labels") or []
    labels_text = details.get("labels_text") or "Brak danych"
    genres_text = details.get("genres_text") or "Brak danych"
    secondary_genres = details.get("secondary_genres") or []
    secondary_genres_text = details.get("secondary_genres_text") or "Brak danych"
    vibes = details.get("vibes") or []
    vibes_text = details.get("vibes_text") or "Brak danych"
    ranking_year = details.get("ranking_year") or "Brak danych"
    year_ranking = details.get("year_ranking") or "Brak danych"
    year_ranking_text = details.get("year_ranking_text") or "Brak danych"
    tracklist = details.get("tracklist") or []
    tracklist_text = details.get("tracklist_text") or "Brak danych"

    embed = discord.Embed(
        title=f"{score_icon(score)} {artist} • **{album}** ({year})",
        url=url,
        description=all_genres_text,
        color=score_color(score),
    )

    embed.add_field(
        name=f"⭐ **{score}**",
        value=" ",
        inline=True,
    )

    if avatar:
        embed.set_author(
            name=username,
            url=f"https://www.albumoftheyear.org/user/{username}",
            icon_url=avatar,
        )
    else:
        embed.set_author(
            name=username,
            url=f"https://www.albumoftheyear.org/user/{username}",
        )

    if cover:
        embed.set_thumbnail(url=cover)

    embed.set_footer(
        text=f"•  {date}",
        icon_url=(
            "https://encrypted-tbn0.gstatic.com/images?"
            "q=tbn:ANd9GcSiJt1MSjldtmrIaTGoE2r3CgsaPB8l1UneW-j9w103bSS5ft45C-OLTCg&s=10"
        ),
    )

    return embed


def setup_recent_command(
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
        name="recent",
        description="Pokazuje 5 ostatnich ocen użytkownika AOTY",
    )
    @discord.app_commands.describe(
        username="Nazwa użytkownika na AOTY",
    )
    async def recent(
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

            ratings = await asyncio.to_thread(
                get_ratings,
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

        if not ratings:
            await interaction.followup.send(
                f"❌ Nie znaleziono ocen użytkownika **{username}**."
            )
            return

        avatar = None

        try:
            avatar = await asyncio.to_thread(
                get_user_avatar,
                username,
            )
        except Exception:
            pass

        recent_ratings = ratings[:5]
        embeds = []

        for item in recent_ratings:
            details = {}

            try:
                details = await asyncio.to_thread(
                    get_album_details,
                    item["url"],
                )
            except AOTYRateLimit:
                await interaction.followup.send(
                    "⚠️ AOTY chwilowo ogranicza liczbę zapytań."
                )
                return
            except Exception:
                details = {}

            embeds.append(
                _rating_embed(
                    username=username,
                    item=item,
                    avatar=avatar,
                    details=details,
                    score_color=score_color,
                    score_icon=score_icon,
                )
            )

        await interaction.followup.send(
            embeds=embeds,
        )
