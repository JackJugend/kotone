import asyncio

import discord
import requests

import aoty
from settings import AOTY_ICON_ATTACHMENT, RATING_FORMATS
from shared import (
    load_release_variables,
    rating_flags_text,
    score_color,
    score_icon,
    username_autocomplete,
)
from views import SingleRatingView


def setup_last_command(tree: discord.app_commands.CommandTree):
    format_choices = [
        discord.app_commands.Choice(name="Wszystkie formaty", value="all")
    ] + [
        discord.app_commands.Choice(name=info["label"], value=key)
        for key, info in RATING_FORMATS.items()
    ]

    @tree.command(
        name="last",
        description="Pokazuje ostatnią ocenę użytkownika AOTY",
    )
    @discord.app_commands.describe(
        username="Użytkownik AOTY",
        format="Opcjonalnie: tylko konkretny format wydania",
    )
    @discord.app_commands.autocomplete(username=username_autocomplete)
    @discord.app_commands.choices(format=format_choices)
    async def last_command(
        interaction: discord.Interaction,
        username: str,
        format: str = "all",
    ):
        await interaction.response.defer()
        username = username.strip()

        try:
            if not await asyncio.to_thread(aoty.aoty_user_exists, username):
                await interaction.followup.send(
                    f"❌ Konto AOTY **{username}** nie istnieje."
                )
                return

            ratings = await asyncio.to_thread(
                aoty.get_recent_ratings,
                username,
                1,
                format,
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
            missing="?",
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
            live_extra = await asyncio.to_thread(
                aoty.get_user_rating_for_album,
                username,
                latest.get("album_id"),
                latest.get("url"),
                latest.get("release_format"),
                10,
                latest.get("review_url"),
                latest.get("album"),
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
            avatar = await asyncio.to_thread(
                aoty.get_user_avatar,
                username,
            )
        except Exception:
            avatar = None

        flags = rating_flags_text(latest)
        footer_flags = f"  •  {flags}" if flags else ""

        # W aktualnym wyglądzie brak secondary genres / vibes daje pustą
        # linię, nie znak zapytania. Zachowujemy to 1:1.
        secondary_genres_display = (
            variables.secondary_genres_text
            if variables.secondary_genres
            else " "
        )
        vibes_display = variables.vibes_text if variables.vibes else " "

        # Wygląd zachowany z obecnej wersji /last.
        embed = discord.Embed(
            title=(
                f"\\{score_icon(variables.score)} "
                f"{variables.display_artist} — **{variables.display_album}** "
                f"({variables.year})"
            ),
            url=variables.url,
            description=(
                f"# — \\⭐ **{variables.score}** \\⭐ — \n"
                f"{variables.all_genres_text}\n"
                f"{secondary_genres_display}\n"
                f"{vibes_display}"
            ),
            color=score_color(variables.score),
        )

        embed.add_field(
            name=(
                f"\\👥 **{variables.aoty_user_score}**/"
                f"{variables.ratings_count}"
            ),
            value=" ",
            inline=True,
        )
        embed.add_field(
            name=f"\\📅 **{variables.year_ranking_text}**",
            value=" ",
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

        embed.set_footer(
            text=(
                f"{variables.album_format}  •  {variables.release_date}  •  "
                f"{variables.labels_text}{footer_flags}"
            ),
            icon_url=AOTY_ICON_ATTACHMENT,
        )


        # Dodatkowy tab: Szczegóły.
        details_lines = [
            (
                f"**AOTY User Score:** "
                f"{variables.aoty_user_score}"
            ),
            (
                f"**Ratings:** "
                f"{variables.ratings_count}"
            ),
            (
                f"**Release date:** "
                f"{variables.release_date}"
            ),
            (
                f"**Format:** "
                f"{variables.album_format}"
            ),
            (
                f"**Label:** "
                f"{variables.labels_text}"
            ),
            (
                f"**Genre:** "
                f"{variables.genres_text}"
            ),
        ]

        if variables.secondary_genres:
            details_lines.append(
                (
                    f"**Secondary genres:** "
                    f"{', '.join(variables.secondary_genres)}"
                )
            )

        if variables.vibes:
            details_lines.append(
                (
                    f"**Vibes:** "
                    f"{', '.join(variables.vibes)}"
                )
            )

        if (
            variables.year_ranking_text
            and variables.year_ranking_text != "?"
        ):
            details_lines.append(
                (
                    f"**{variables.ranking_year or variables.year} "
                    f"Ratings:** "
                    f"{variables.year_ranking_text}"
                )
            )

        details_embed = discord.Embed(
            title=(
                f"ℹ {variables.display_artist} — "
                f"{variables.display_album}"
            ),
            url=variables.url,
            description="\n".join(
                details_lines
            ),
            color=score_color(
                variables.score
            ),
        )

        if variables.cover:
            details_embed.set_thumbnail(
                url=variables.cover
            )

        details_embed.set_author(
            name=f"{username}  •  {variables.date}",
            url=f"https://www.albumoftheyear.org/user/{username}",
            icon_url=avatar if avatar else None,
        )

        details_embed.set_footer(
            text=(
                f"AOTY • "
                f"{score_icon(variables.score)} "
                f"{variables.score}"
            ),
            icon_url=AOTY_ICON_ATTACHMENT,
        )

        # Dodatkowy tab: publiczna tracklista.
        track_lines = []

        for track in variables.tracklist:
            number = track.get("number") or "?"
            title = track.get("title") or "Nieznany utwór"
            duration = track.get("duration")
            public_score = track.get("user_score") or "NR"

            line = f"**{number}.** {title}"

            if duration:
                line += f" `{duration}`"

            line += f" — **{public_score}**"
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

        tracklist_embed.set_footer(
            text=(
                "AOTY track scores • "
                f"{variables.album_format}"
            ),
            icon_url=AOTY_ICON_ATTACHMENT,
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
