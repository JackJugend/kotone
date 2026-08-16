import asyncio
import time

import discord
import requests

import aoty
from display_utils import display_romanized_name
from settings import USERS
from shared import (
    build_release_variables,
    rating_flags_text,
    score_color,
    score_icon,
)
from views import AlbumRatingView

DISCOGRAPHY_CACHE_TTL = 900


def setup_album_command(tree: discord.app_commands.CommandTree):
    discography_cache = {}

    async def _discography_for(artist_value):
        key = str(artist_value or "").strip().casefold()
        now = time.monotonic()
        cached = discography_cache.get(key)

        if cached and now - cached[0] < DISCOGRAPHY_CACHE_TTL:
            return cached[1], cached[2]

        artist_info = await asyncio.to_thread(aoty.resolve_artist, artist_value)
        if not artist_info:
            return None, None

        discography = await asyncio.to_thread(
            aoty.get_artist_releases,
            artist_info["url"],
        )

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

        try:
            results = await asyncio.to_thread(aoty.search_aoty_artists, current, 10)
        except Exception:
            return []

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
        description="Pokazuje wydanie z AOTY i oceny userów z configu",
    )
    @discord.app_commands.describe(
        artist="Artysta na AOTY",
        album="Wydanie — nazwa może być niedokładna",
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
            direct_choice = str(album).startswith("aoty_album:")

            if not direct_choice and match_score < 0.28:
                await interaction.followup.send(
                    f"❌ Nie znaleziono wystarczająco podobnego wydania do **{album}**."
                )
                return

            details = await asyncio.to_thread(aoty.get_album_details, release["url"])

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
        release_item["release_format"] = (
            details.get("album_format")
            or release.get("album_format")
        )

        variables = build_release_variables(release_item, details)

        embed = discord.Embed(
            title=(
                f"{score_icon(variables.aoty_user_score)} "
                f"{variables.display_artist} • **{variables.display_album}** "
                f"({variables.year})"
            ),
            url=variables.url,
            description=variables.all_genres_text,
            color=score_color(variables.aoty_user_score),
        )

        # Zawsze live. Każda pozycja zawiera też informację o review,
        # track ratings i like, jeśli AOTY udostępnia ją na stronie usera.
        rating_infos: dict[str, dict] = {}

        for username in USERS[:25]:
            try:
                rating_info = await asyncio.to_thread(
                    aoty.get_user_rating_for_album,
                    username,
                    release["album_id"],
                    release["url"],
                    variables.album_format,
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
                rating_value = f"{score_icon(rating)} **{rating}**{flags_text}"
            else:
                rating_value = f"— **NR**{flags_text}"

            embed.add_field(
                name=username,
                value=rating_value,
                inline=True,
            )
            await asyncio.sleep(0.15)

        if variables.cover:
            embed.set_thumbnail(url=variables.cover)

        embed.set_footer(
            text=f"•  {variables.release_date}  •  {variables.album_format}"
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
