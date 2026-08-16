import asyncio
import discord
import requests

def setup_last_command(
    tree,
    get_ratings,
    get_user_avatar,
    score_color,
    score_icon,
    AOTYRateLimit,
    AOTYUserNotFound,
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
            ratings = await asyncio.to_thread(
                get_ratings,
                username
            )

        except AOTYRateLimit:
            await interaction.followup.send(
                "⚠️ AOTY chwilowo ogranicza liczbę zapytań."
            )
            return
        
        except AOTYUserNotFound:

            await interaction.followup.send(
                f"❌ Konto AOTY **{username}** nie istnieje."
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

        embed = discord.Embed(
                title=f"{album}",
                url=url,
                description=f"**{artist}**",
                color=score_color(score),
        )

        embed.add_field(
                name=f"**{score}**  {score_icon(score)}",
                value=" ",
                inline=True
        )
        
        if avatar:
            embed.set_author(
                name=username,
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
            text=f"{date}  🔥"
        )

        await interaction.followup.send(
            embed=embed
        )