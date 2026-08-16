import asyncio

import discord
import requests


def _favorite_line(item):

    url = item.get("url")
    item_type = item.get("type")

    if item_type == "artist":
        name = item.get("name") or "Nieznany artysta"
        return f"• 🎤 **[{name}]({url})**"

    album = item.get("album") or item.get("name") or "Nieznane wydanie"
    artist = item.get("artist")

    if artist:
        return f"• 💿 **[{artist} — {album}]({url})**"

    return f"• 💿 **[{album}]({url})**"


def _recent_line(item, score_icon):

    artist = item.get("artist") or "Nieznany artysta"
    album = item.get("album") or "Nieznane wydanie"
    score = item.get("score") or "NR"
    url = item.get("url")
    release_format = item.get("release_format") or "?"

    return (
        f"• {score_icon(score)} **{score}** "
        f"[{artist} — {album}]({url}) · {release_format}"
    )


def setup_profile_command(
    tree,
    get_profile_data,
    aoty_user_exists,
    score_color,
    score_icon,
    AOTYRateLimit,
):
    @tree.command(
        name="profile",
        description="Pokazuje profil użytkownika AOTY",
    )
    @discord.app_commands.describe(
        username="Nazwa użytkownika na AOTY",
    )
    async def profile_command(
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

            profile = await asyncio.to_thread(
                get_profile_data,
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

        display_username = profile.get("username") or username
        avatar = profile.get("avatar")
        profile_url = profile.get("url")

        ratings_count = profile.get("ratings_count") or "0"
        reviews_count = profile.get("reviews_count") or "0"
        lists_count = profile.get("lists_count") or "0"
        following_count = profile.get("following_count") or "0"
        followers_count = profile.get("followers_count") or "0"
        average_rating_text = (
            profile.get("average_rating_text")
            or "Brak danych"
        )

        favorites = profile.get("favorites") or []
        recent_ratings = profile.get("recent_ratings") or []

        favorite_lines = [
            _favorite_line(item)
            for item in favorites[:5]
        ]

        recent_lines = [
            _recent_line(item, score_icon)
            for item in recent_ratings[:5]
        ]

        average_rating = profile.get("average_rating")

        embed_color = None

        if average_rating is not None:
            embed_color = score_color(
                round(average_rating)
            )

        embed = discord.Embed(
            title=display_username,
            url=profile_url,
            description=(
                f"**{ratings_count}** ocen  •  "
                f"średnia albumów **{average_rating_text}**"
            ),
            color=embed_color,
        )

        if avatar:
            embed.set_author(
                name=display_username,
                url=profile_url,
                icon_url=avatar,
            )
            embed.set_thumbnail(
                url=avatar,
            )
        else:
            embed.set_author(
                name=display_username,
                url=profile_url,
            )

        embed.add_field(
            name="Statystyki",
            value=(
                f"Reviews **{reviews_count}**  •  "
                f"Lists **{lists_count}**\n"
                f"Following **{following_count}**  •  "
                f"Followers **{followers_count}**"
            ),
            inline=False,
        )

        embed.add_field(
            name="Top 5",
            value=(
                "\n".join(favorite_lines)
                if favorite_lines
                else "—"
            ),
            inline=False,
        )

        embed.add_field(
            name="Ostatnie 5 ocen",
            value=(
                "\n".join(recent_lines)
                if recent_lines
                else "—"
            ),
            inline=False,
        )

        embed.set_footer(
            text="AOTY.org • średnia jest przybliżona z Rating Distribution",
        )

        await interaction.followup.send(
            embed=embed,
        )
