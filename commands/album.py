import asyncio
import time
import discord
import requests


DISCOGRAPHY_CACHE_TTL = 900


def setup_album_command(
    tree,
    users,
    search_aoty_artists,
    resolve_artist,
    get_artist_releases,
    rank_artist_releases,
    resolve_album_for_artist,
    get_album_details,
    get_user_rating_for_album,
    score_color,
    score_icon,
    AOTYRateLimit,
):
    discography_cache = {}

    async def _discography_for(artist_value):
        key = str(artist_value or "").strip().casefold()
        now = time.monotonic()
        cached = discography_cache.get(key)

        if cached and now - cached[0] < DISCOGRAPHY_CACHE_TTL:
            return cached[1], cached[2]

        artist_info = await asyncio.to_thread(
            resolve_artist,
            artist_value,
        )

        if not artist_info:
            return None, None

        discography = await asyncio.to_thread(
            get_artist_releases,
            artist_info["url"],
        )

        discography_cache[key] = (
            now,
            artist_info,
            discography,
        )

        # Choice z autocomplete ma inną wartość niż widoczna nazwa.
        # Cache'ujemy oba warianty.
        choice_value = artist_info.get("value")
        if choice_value:
            discography_cache[choice_value.casefold()] = (
                now,
                artist_info,
                discography,
            )

        artist_name = artist_info.get("name")
        if artist_name:
            discography_cache[artist_name.casefold()] = (
                now,
                artist_info,
                discography,
            )

        return artist_info, discography

    async def artist_autocomplete(
        interaction: discord.Interaction,
        current: str,
    ):
        if not current or len(current.strip()) < 2:
            return []

        try:
            results = await asyncio.to_thread(
                search_aoty_artists,
                current,
                10,
            )
        except Exception:
            return []

        return [
            discord.app_commands.Choice(
                name=item["name"][:100],
                value=item["value"][:100],
            )
            for item in results[:10]
        ]

    async def album_autocomplete(
        interaction: discord.Interaction,
        current: str,
    ):
        artist_value = getattr(
            interaction.namespace,
            "artist",
            None,
        )

        if not artist_value:
            return []

        try:
            artist_info, discography = await _discography_for(
                artist_value
            )

            if not artist_info or not discography:
                return []

            releases = discography.get("releases", [])

            if current.strip():
                ranked = rank_artist_releases(
                    releases,
                    current,
                )
                releases = [
                    release
                    for _, release in ranked
                ]

            choices = []

            for release in releases[:25]:
                title = release.get("title", "")
                year = release.get("year")
                release_format = release.get("album_format")

                suffix_parts = [
                    part
                    for part in (year, release_format)
                    if part
                ]

                if suffix_parts:
                    choice_name = (
                        f"{title} ({' · '.join(suffix_parts)})"
                    )
                else:
                    choice_name = title

                choices.append(
                    discord.app_commands.Choice(
                        name=choice_name[:100],
                        value=(
                            "aoty_album:"
                            + str(release["album_id"])
                        )[:100],
                    )
                )

            return choices

        except Exception:
            return []

    @tree.command(
        name="album",
        description="Pokazuje album i oceny monitorowanych użytkowników AOTY",
    )
    @discord.app_commands.describe(
        artist="Artysta na AOTY",
        album="Album — możesz wpisać nazwę niedokładnie",
    )
    @discord.app_commands.autocomplete(
        artist=artist_autocomplete,
        album=album_autocomplete,
    )
    async def album_command(
        interaction: discord.Interaction,
        artist: str,
        album: str,
    ):
        await interaction.response.defer()

        try:
            artist_info, discography = await _discography_for(
                artist
            )

            if not artist_info or not discography:
                await interaction.followup.send(
                    f"❌ Nie znaleziono artysty **{artist}** na AOTY."
                )
                return

            ranked = rank_artist_releases(
                discography.get("releases", []),
                album,
            )

            if not ranked:
                await interaction.followup.send(
                    f"❌ Nie znaleziono albumu **{album}** u tego artysty."
                )
                return

            match_score, release = ranked[0]
            direct_choice = str(album).startswith("aoty_album:")

            if not direct_choice and match_score < 0.28:
                await interaction.followup.send(
                    f"❌ Nie znaleziono wystarczająco podobnego albumu do **{album}**."
                )
                return

            details = await asyncio.to_thread(
                get_album_details,
                release["url"],
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

        artist_name = discography.get("artist") or artist_info["name"]
        album_title = release["title"]
        album_url = release["url"]
        cover = release.get("cover")

        # Te same zmienne szczegółów co w /last.
        user_score = details.get("user_score") or "Brak danych"
        aoty_user_score = user_score
        ratings_count = details.get("ratings_count") or "Brak danych"

        release_date = details.get("release_date") or "Brak danych"
        year = details.get("year") or release.get("year") or "Brak danych"
        album_format = details.get("album_format") or release.get("album_format") or "Brak danych"

        label = details.get("label") or "Brak danych"
        labels = details.get("labels") or []
        labels_text = details.get("labels_text") or "Brak danych"

        genres = details.get("genres") or []
        genres_text = details.get("genres_text") or "Brak danych"
        main_genre = genres[0] if genres else "Brak danych"
        other_genres = ", ".join(genres[1:]) if len(genres) > 1 else "Brak danych"
        other_genres_text = other_genres

        if genres:
            if len(genres) > 1:
                all_genres_text = f"**{main_genre}**, {other_genres_text}"
            else:
                all_genres_text = f"**{main_genre}**"
        else:
            all_genres_text = "Brak danych"

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
            title=(
                f"{score_icon(aoty_user_score)} "
                f"{artist_name} • **{album_title}** ({year})"
            ),
            url=album_url,
            description=all_genres_text,
            color=score_color(aoty_user_score),
        )

        # Oceny wszystkich użytkowników wpisanych w config.json.
        for username in users:
            rating_info = {
                "score": None,
                "date": None,
            }

            try:
                rating_info = await asyncio.to_thread(
                    get_user_rating_for_album,
                    username,
                    release["album_id"],
                )
            except AOTYRateLimit:
                pass
            except Exception:
                pass

            rating = rating_info.get("score")

            if rating is not None:
                rating_value = (
                    f"{score_icon(rating)} **{rating}**"
                )
            else:
                rating_value = "— **NR**"

            embed.add_field(
                name=username,
                value=rating_value,
                inline=True,
            )

        if cover:
            embed.set_thumbnail(
                url=cover,
            )

        embed.set_footer(
            text=f"•  {release_date}",
        )

        await interaction.followup.send(
            embed=embed,
        )
