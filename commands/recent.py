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
from views import MultiRatingView


def _rating_embed(username, item, avatar, variables):
    flags = rating_flags_text(item)
    footer_flags = f"  •  {flags}" if flags else ""

    # /recent intentionally shares the primary-card language of /last.  It
    # omits only Secondary Genres; all release flags remain attached to this
    # exact rating in the shared footer.
    description_lines = [f"# — \⭐ **{variables.score}** \⭐ —"]
    if variables.genres:
        description_lines.append(variables.all_genres_text)

    embed = discord.Embed(
        title=(
            f"{score_icon(variables.score)} "
            f"{variables.display_artist} — "
            f"**{variables.display_album}**{release_year_suffix(variables.year)}"
        ),
        url=variables.url,
        description="\n".join(description_lines),
        color=score_color(variables.score),
    )

    embed.add_field(
        name=f"\👥 **{variables.aoty_user_score}**/{variables.ratings_count}",
        value=" ",
        inline=True,
    )
    embed.add_field(
        name=f"\📅 **{variables.year_ranking_text}**",
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
        embed.set_author(
            name=f"{username}  •  {variables.date}",
            url=f"https://www.albumoftheyear.org/user/{username}",
        )

    if variables.cover:
        embed.set_thumbnail(url=variables.cover)

    set_aoty_footer(
        embed,
        f"{variables.album_format}  •  {variables.release_date}  •  "
        f"{variables.labels_text}{footer_flags}",
    )
    return embed


def setup_recent_command(tree: discord.app_commands.CommandTree):
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
        name="recent",
        description="Pokazuje od 1 do 20 ostatnich ocen użytkownika AOTY",
    )
    @discord.app_commands.describe(
        username="Użytkownik AOTY",
        amount="Ile ostatnich ocen pokazać (1-20)",
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
    async def recent_command(
        interaction: discord.Interaction,
        username: str,
        amount: discord.app_commands.Range[int, 1, 20] = 5,
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
                int(amount),
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
            await interaction.followup.send(
                f"❌ Nie znaleziono ocen użytkownika **{username}**."
            )
            return

        try:
            avatar = await DATA.get_avatar(username)
        except Exception:
            avatar = None

        ratings = ratings[:int(amount)]
        embeds = []

        for item in ratings:
            variables = await load_release_variables(
                item,
                username=username,
            )

            embeds.append(
                _rating_embed(
                    username,
                    item,
                    avatar,
                    variables,
                )
            )
            await asyncio.sleep(0.12)

        # Discord: max 10 embeds na wiadomość. Każda partia ma własny select
        # do wyboru oceny, której recenzję/track ratings chcemy zobaczyć.
        for start in range(0, len(embeds), 10):
            batch_embeds = embeds[start:start + 10]
            batch_items = ratings[start:start + 10]
            view = MultiRatingView(
                username=username,
                items=batch_items,
                main_embeds=batch_embeds,
            )
            message = await interaction.followup.send(
                embeds=batch_embeds,
                view=view,
                wait=True,
            )
            view.bind_message(message)
