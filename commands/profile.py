import asyncio

import discord
import requests

import aoty
from services import DATA
from display_utils import display_romanized_name
from settings import AOTY_ICON_ATTACHMENT
from shared import (
    build_profile_variables,
    rating_flags_text,
    score_color,
    score_icon,
    username_autocomplete,
)
from views import ProfilePagerView


def _favorite_line(item: dict) -> str:
    url = item.get("url")
    item_type = item.get("type")

    if item_type == "artist":
        name = display_romanized_name(item.get("name") or "Nieznany artysta")
        return f"\\⭐ **[{name}]({url})**"

    album = display_romanized_name(
        item.get("album") or item.get("name") or "Nieznane wydanie"
    )
    artist = item.get("artist")
    display_artist = display_romanized_name(artist) if artist else None

    if display_artist:
        return f"\\💿 **[{display_artist} — {album}]({url})**"

    return f"\\💿 **[{album}]({url})**"


def _recent_line(item: dict) -> str:
    artist = display_romanized_name(item.get("artist") or "Nieznany artysta")
    album = display_romanized_name(item.get("album") or "Nieznane wydanie")
    score = item.get("score") or "NR"
    url = item.get("url")
    release_format = item.get("release_format") or "?"
    flags = rating_flags_text(item)
    flags_text = f" · {flags}" if flags else ""

    return (
        f"\\{score_icon(score)} **{score}** · "
        f"[{artist} — {album}]({url}) · {release_format}{flags_text}"
    )


def setup_profile_command(tree: discord.app_commands.CommandTree):
    @tree.command(
        name="profile",
        description="Pokazuje profil użytkownika AOTY",
    )
    @discord.app_commands.describe(username="Użytkownik AOTY")
    @discord.app_commands.autocomplete(username=username_autocomplete)
    async def profile_command(
        interaction: discord.Interaction,
        username: str,
    ):
        await interaction.response.defer()
        username = username.strip()

        try:
            if not await DATA.user_exists(username):
                await interaction.followup.send(
                    f"❌ Konto AOTY **{username}** nie istnieje."
                )
                return

            profile = await DATA.get_profile(
                username,
                recent_limit=50,
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

        variables = build_profile_variables(profile, username)

        # Te same nazwy co wcześniej pozostają czytelne w embedzie; ich
        # defaulty/źródło są jednak centralnie zdefiniowane w shared.py.
        display_username = variables.display_username
        avatar = variables.avatar
        profile_url = variables.profile_url
        ratings_count = variables.ratings_count
        reviews_count = variables.reviews_count
        lists_count = variables.lists_count
        following_count = variables.following_count
        followers_count = variables.followers_count
        average_rating_text = variables.average_rating_text
        average_rating = variables.average_rating
        favorites = variables.favorites[:5]
        favorite_kind = variables.favorite_kind

        if favorite_kind == "artists":
            favorites_field_name = "Favorite Artists"
        elif favorite_kind == "albums":
            favorites_field_name = "Favorite Albums"
        else:
            favorites_field_name = "Favorites"

        favorite_lines = [_favorite_line(item) for item in favorites]
        recent_ratings = variables.recent_ratings[:50]
        total_pages = max(1, min(10, (len(recent_ratings) + 4) // 5))

        embed_color = score_color(round(average_rating)) if average_rating is not None else None

        def build_page_embed(page_index: int) -> discord.Embed:
            page_index = max(0, min(page_index, total_pages - 1))
            start = page_index * 5
            page_items = recent_ratings[start:start + 5]
            recent_lines = [_recent_line(item) for item in page_items]

            embed = discord.Embed(
                title=display_username,
                url=profile_url,
                description=(
                    f"**{ratings_count}** ocen  •  "
                    f"x̄ **{average_rating_text}**"
                ),
                color=embed_color,
            )

            embed.add_field(
                name=" ",
                value=(
                    f"Reviews **{reviews_count}**  •  "
                    f"Lists **{lists_count}**\n"
                    f"Following **{following_count}**  •  "
                    f"Followers **{followers_count}**"
                ),
                inline=False,
            )

            if avatar:
                embed.set_author(
                    name=display_username,
                    url=profile_url,
                    icon_url=avatar,
                )
                embed.set_thumbnail(url=avatar)
            else:
                embed.set_author(name=display_username, url=profile_url)

            # Zgodnie z ustawieniem profilu AOTY pokazujemy tylko ten typ
            # Favorites, który user ma wybrany jako domyślny.
            embed.add_field(
                name=favorites_field_name,
                value="\n".join(favorite_lines) if favorite_lines else "—",
                inline=False,
            )

            embed.add_field(
                name=f"Ostatnie 5 ocen [{page_index + 1}/{total_pages}]",
                value="\n".join(recent_lines) if recent_lines else "—",
                inline=False,
            )

            embed.set_footer(
                text="AOTY.org • średnia jest przybliżona z Rating Distribution",
                icon_url=AOTY_ICON_ATTACHMENT,
            )
            return embed

        first_embed = build_page_embed(0)
        view = ProfilePagerView(
            username=username,
            ratings=recent_ratings,
            build_page_embed=build_page_embed,
        )

        message = await interaction.followup.send(
            embed=first_embed,
            view=view,
            wait=True,
        )
        view.bind_message(message)
