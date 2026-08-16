import asyncio

import discord
import requests

import aoty
from shared import (
    load_release_variables,
    rating_flags_text,
    score_color,
    score_icon,
    username_autocomplete,
)
from views import MultiRatingView


def _rating_embed(username, item, avatar, variables):
    flags = rating_flags_text(item)
    footer_flags = f"  •  {flags}" if flags else ""

    embed = discord.Embed(
        title=(
            f"{score_icon(variables.score)} {variables.display_artist} • "
            f"**{variables.display_album}** ({variables.year})"
        ),
        url=variables.url,
        description=variables.all_genres_text,
        color=score_color(variables.score),
    )

    embed.add_field(
        name=f"⭐ **{variables.score}**",
        value=" ",
        inline=True,
    )
    embed.add_field(
        name=f"👥 **{variables.aoty_user_score}**/{variables.ratings_count}",
        value=" ",
        inline=True,
    )
    embed.add_field(
        name=f"📅 **{variables.year_ranking_text}**",
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

    if variables.cover:
        embed.set_thumbnail(url=variables.cover)

    embed.set_footer(
        text=f"•  {variables.date}  •  {variables.album_format}{footer_flags}"
    )
    return embed


def setup_recent_command(tree: discord.app_commands.CommandTree):
    @tree.command(
        name="recent",
        description="Pokazuje od 1 do 20 ostatnich ocen użytkownika AOTY",
    )
    @discord.app_commands.describe(
        username="Użytkownik AOTY",
        amount="Ile ostatnich ocen pokazać (1-20)",
    )
    @discord.app_commands.autocomplete(username=username_autocomplete)
    async def recent_command(
        interaction: discord.Interaction,
        username: str,
        amount: discord.app_commands.Range[int, 1, 20] = 5,
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
                int(amount),
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
            avatar = await asyncio.to_thread(aoty.get_user_avatar, username)
        except Exception:
            avatar = None

        ratings = ratings[:int(amount)]
        embeds = []

        for item in ratings:
            variables = await load_release_variables(
                item,
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
