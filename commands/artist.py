import asyncio

import discord
import requests

import aoty
from display_utils import display_romanized_name
from settings import RATING_FORMATS
from shared import build_release_variables

MAX_ARTIST_RELEASES = 18


def setup_artist_command(tree: discord.app_commands.CommandTree):
    format_choices = [
        discord.app_commands.Choice(name="Wszystkie formaty", value="all")
    ] + [
        discord.app_commands.Choice(name=info["label"], value=key)
        for key, info in RATING_FORMATS.items()
    ]

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

    @tree.command(
        name="artist",
        description="Pokazuje dyskografię artysty z datami i ocenami AOTY",
    )
    @discord.app_commands.describe(
        artist="Nazwa artysty na AOTY",
        format="Format wydań, które chcesz zobaczyć",
    )
    @discord.app_commands.autocomplete(artist=artist_autocomplete)
    @discord.app_commands.choices(format=format_choices)
    async def artist_command(
        interaction: discord.Interaction,
        artist: str,
        format: str = "all",
    ):
        await interaction.response.defer()

        try:
            artist_info = await asyncio.to_thread(aoty.resolve_artist, artist)

            if not artist_info:
                await interaction.followup.send(
                    f"❌ Nie znaleziono artysty **{artist}** na AOTY."
                )
                return

            discography = await asyncio.to_thread(
                aoty.get_artist_releases,
                artist_info["url"],
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

        releases = list(discography.get("releases", []))
        selected_label = "Wszystkie formaty"

        if format != "all":
            info = RATING_FORMATS.get(format)
            if not info:
                await interaction.followup.send("❌ Nieznany format.")
                return

            selected_label = info["label"]
            wanted = selected_label.casefold()
            releases = [
                item
                for item in releases
                if (item.get("album_format") or "").casefold() == wanted
            ]

        if not releases:
            await interaction.followup.send(
                f"❌ Nie znaleziono wydań artysty **{discography['artist']}** "
                f"dla formatu **{selected_label}**."
            )
            return

        shown = releases[:MAX_ARTIST_RELEASES]
        lines = []

        for release in shown:
            try:
                details = await asyncio.to_thread(aoty.get_album_details, release["url"])
            except Exception:
                details = {}

            variables = build_release_variables(release, details)
            lines.append(
                f"• **[{variables.display_album}]({release['url']})**"
                f" — {variables.release_date} · {variables.album_format}"
                f" — ⭐ **{variables.aoty_user_score}**"
            )
            await asyncio.sleep(0.12)

        embed = discord.Embed(
            title=display_romanized_name(discography["artist"]),
            url=discography["url"],
            description="\n".join(lines),
        )

        if discography.get("image"):
            embed.set_thumbnail(url=discography["image"])

        if len(releases) > len(shown):
            footer = f"{selected_label} • pokazano {len(shown)} z {len(releases)} wydań."
        else:
            footer = f"{selected_label} • {len(shown)} wydań."

        embed.set_footer(text=footer)
        await interaction.followup.send(embed=embed)
