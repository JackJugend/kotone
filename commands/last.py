import asyncio

import discord
import requests

import aoty
from services import DATA
from settings import RATING_FORMATS
from shared import (
    load_release_variables,
    rating_flags_text,
    release_year_suffix,
    score_color,
    score_icon,
    set_aoty_footer,
    username_autocomplete,
)
from views import SingleRatingView, build_release_details_embed


def setup_last_command(tree: discord.app_commands.CommandTree):
    format_choices = [
        discord.app_commands.Choice(name="Wszystkie formaty", value="all")
    ] + [
        discord.app_commands.Choice(name=info["label"], value=key)
        for key, info in RATING_FORMATS.items()
    ]

    async def genre_autocomplete(interaction: discord.Interaction, current: str):
        username = str(getattr(interaction.namespace, "username", "") or "")
        needle = str(current or "").casefold()
        return [
            discord.app_commands.Choice(name=value[:100], value=value[:100])
            for value in DATA.cached_genres(username)
            if needle in value.casefold()
        ][:25]

    @tree.command(
        name="last",
        description="Pokazuje ostatnią ocenę użytkownika AOTY",
    )
    @discord.app_commands.describe(
        username="Użytkownik AOTY",
        format="Opcjonalnie: tylko konkretny format wydania",
        genre="Gatunek zapisany w kotone",
        year="Rok wydania",
        decade="Początek dekady, np. 2020",
        rating_date="Data oceny, np. 01.05.2026",
        aoty_min="Minimalny AOTY User Score",
        aoty_max="Maksymalny AOTY User Score",
        user_min="Minimalna ocena użytkownika",
        user_max="Maksymalna ocena użytkownika",
    )
    @discord.app_commands.autocomplete(
        username=username_autocomplete,
        genre=genre_autocomplete,
    )
    @discord.app_commands.choices(format=format_choices)
    async def last_command(
        interaction: discord.Interaction,
        username: str,
        format: str = "all",
        genre: str | None = None,
        year: int | None = None,
        decade: int | None = None,
        rating_date: str | None = None,
        aoty_min: int | None = None,
        aoty_max: int | None = None,
        user_min: int | None = None,
        user_max: int | None = None,
    ):
        await interaction.response.defer()
        username = username.strip()

        try:
            if not await DATA.user_exists(username):
                await interaction.followup.send(
                    f"❌ Konto AOTY **{username}** nie istnieje."
                )
                return

            ratings = await DATA.get_recent_ratings(
                username,
                1,
                format,
                allow_network=False,
                genre=genre,
                year=year,
                decade=decade,
                rating_date=rating_date,
                aoty_min=aoty_min,
                aoty_max=aoty_max,
                user_min=user_min,
                user_max=user_max,
            )

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

        if not ratings:
            selected = (
                RATING_FORMATS.get(format, {}).get("label")
                if format != "all"
                else None
            )
            suffix = f" w formacie **{selected}**" if selected else ""
            await interaction.followup.send(
                f"❌ Nie znaleziono ocen użytkownika **{username}**{suffix}."
            )
            return

        latest = ratings[0]

        variables = await load_release_variables(
            latest,
            username=username,
            missing="—",
        )

        if variables.artist_url:
            latest["artist_url"] = variables.artist_url

        if variables.album_url:
            latest["url"] = variables.album_url

        # /last already knows exactly which rating was selected, so fetch its
        # user-specific detail NOW and give it to the View. This removes the
        # old inconsistency where the main embed worked but Track ratings had
        # to search for the same release again after the button was clicked.
        live_extra = None

        try:
            live_extra = await DATA.get_user_rating_for_album(
                username,
                latest.get("album_id"),
                latest.get("url"),
                latest.get("release_format"),
                fallback_limit=10,
                user_release_url=latest.get("review_url"),
                album_title=latest.get("album"),
                require_detail=True,
                allow_network=False,
            )

            # Preserve the exact user-release URL discovered live.
            if live_extra.get("review_url"):
                latest["review_url"] = live_extra.get(
                    "review_url"
                )

            if live_extra.get("has_review"):
                latest["has_review"] = True

            if live_extra.get("has_track_ratings"):
                latest["has_track_ratings"] = True

            if live_extra.get("liked"):
                latest["liked"] = True

        except aoty.AOTYRateLimit:
            # Main /last should still render. The View will retry if the user
            # presses a detail button later.
            live_extra = None

        except Exception as exc:
            print(
                f"[LAST] Nie udało się wstępnie pobrać szczegółów "
                f"{username} / {latest.get('album_id')}: "
                f"{type(exc).__name__}: {exc}"
            )
            live_extra = None

        try:
            avatar = await DATA.get_avatar(username)
        except Exception:
            avatar = None

        flags = rating_flags_text(latest)
        footer_flags = f"  •  {flags}" if flags else ""

        description_lines = [f"# — \⭐ **{variables.score}** \⭐ —"]
        if variables.genres:
            description_lines.append(variables.all_genres_text.title())
        if variables.secondary_genres:
            description_lines.append(f"*{variables.secondary_genres_text.title()}*")
        if variables.vibes:
            description_lines.append(f"-# {variables.vibes_text}")

        # Wygląd zachowany z obecnej wersji /last.
        embed = discord.Embed(
            title=(
                f"{score_icon(variables.score)} "
                f"{variables.display_artist} — **{variables.display_album}**"
                f"{release_year_suffix(variables.year)}"
            ),
            url=variables.url,
            description="\n".join(description_lines),
            color=score_color(variables.score),
        )

        embed.add_field(
            name=(
                f"\📊 **{variables.aoty_user_score}**"
            ),
            value=f"/{variables.ratings_count}",
            inline=True,
        )
        embed.add_field(
            name=f"\🏆 **{variables.year_ranking_text}**",
            value=f"for **{variables.year}**",
            inline=True,
        )

        if avatar:
            embed.set_author(
                name=f"{username}  •  {variables.date}",
                url=f"https://www.albumoftheyear.org/user/{username}",
                icon_url=avatar,
            )
        else:
            embed.set_author(name=f"{username}  •  {variables.date}")

        if variables.cover:
            embed.set_thumbnail(url=variables.cover)

        set_aoty_footer(
            embed,
            f"{variables.album_format}  •  {variables.release_date}  •  "
            f"{variables.labels_text}{footer_flags}",
        )


        # Every command uses the same details renderer and provenance markers.
        details_embed = await build_release_details_embed(
            latest,
            username=username,
            author_icon_url=avatar,
        )

        # Dodatkowy tab: publiczna tracklista.
        track_lines = []

        for track in variables.tracklist:
            number = track.get("number") or "—"
            title = track.get("title") or "Nieznany utwór"
            duration = track.get("duration")
            public_score = track.get("user_score")

            line = f"**{number}.** {title}"

            if duration:
                line += f" `{duration}`"

            line += f" — **{score_or_nr(public_score)}**"
            track_lines.append(line)

        if not track_lines:
            track_lines = [
                "Brak tracklisty na AOTY."
            ]

        tracklist_embed = discord.Embed(
            title=(
                f"≡ {variables.display_artist} — "
                f"{variables.display_album}"
            ),
            url=variables.url,
            description="\n".join(
                track_lines
            )[:4000],
            color=score_color(
                variables.score
            ),
        )

        if variables.cover:
            tracklist_embed.set_thumbnail(
                url=variables.cover
            )

        tracklist_embed.set_author(
            name=f"{username}  •  {variables.date}",
            url=f"https://www.albumoftheyear.org/user/{username}",
            icon_url=avatar if avatar else None,
        )

        set_aoty_footer(
            tracklist_embed,
            f"AOTY track scores • {variables.album_format}",
        )

        view = SingleRatingView(
            username=username,
            item=latest,
            main_embed=embed,
            extra=(
                live_extra
                if (
                    live_extra
                    and not live_extra.get(
                        "detail_incomplete"
                    )
                )
                else None
            ),
            details_embed=details_embed,
            tracklist_embed=tracklist_embed,
            artist_url=variables.artist_url or None,
            album_url=variables.album_url or variables.url or None,
        )

        message = await interaction.followup.send(
            embed=embed,
            view=view,
            wait=True,
        )
        view.bind_message(message)
