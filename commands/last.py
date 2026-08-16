import asyncio

import discord
import requests

import aoty
from settings import AOTY_ICON_ATTACHMENT, RATING_FORMATS
from shared import (
    build_release_variables,
    make_aoty_file,
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

        try:
            details = await asyncio.to_thread(
                aoty.get_album_details,
                latest["url"],
            )
        except Exception:
            details = {}

        variables = build_release_variables(latest, details, missing="?")

        try:
            avatar = await asyncio.to_thread(aoty.get_user_avatar, username)
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

        view = SingleRatingView(
            username=username,
            item=latest,
            main_embed=embed,
        )

        await interaction.followup.send(
            embed=embed,
            file=make_aoty_file(),
            view=view,
        )
