import asyncio

import discord
import requests


MAX_ARTIST_RELEASES = 18


def setup_artist_command(
    tree,
    rating_formats,
    search_aoty_artists,
    resolve_artist,
    get_artist_releases,
    get_album_details,
    AOTYRateLimit,
):
    format_choices = [
        discord.app_commands.Choice(
            name="Wszystkie formaty",
            value="all",
        )
    ]

    for key, info in rating_formats.items():
        format_choices.append(
            discord.app_commands.Choice(
                name=info["label"],
                value=key,
            )
        )

    async def artist_autocomplete(
        interaction: discord.Interaction,
        current: str,
    ):
        if not current or len(current.strip()) < 2:
            return []

        try:
            results = await asyncio.to_thread(
                search_aoty_artists,
                current,
                10,
            )
        except Exception:
            return []

        return [
            discord.app_commands.Choice(
                name=item["name"][:100],
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
    @discord.app_commands.autocomplete(
        artist=artist_autocomplete,
    )
    @discord.app_commands.choices(
        format=format_choices,
    )
    async def artist_command(
        interaction: discord.Interaction,
        artist: str,
        format: str = "all",
    ):
        await interaction.response.defer()

        try:
            artist_info = await asyncio.to_thread(
                resolve_artist,
                artist,
            )

            if not artist_info:
                await interaction.followup.send(
                    f"❌ Nie znaleziono artysty **{artist}** na AOTY."
                )
                return

            discography = await asyncio.to_thread(
                get_artist_releases,
                artist_info["url"],
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

        releases = list(
            discography.get(
                "releases",
                []
            )
        )

        selected_label = "Wszystkie formaty"

        if format != "all":
            format_info = rating_formats.get(format)

            if not format_info:
                await interaction.followup.send(
                    "❌ Nieznany format."
                )
                return

            selected_label = format_info["label"]
            wanted = selected_label.casefold()

            releases = [
                item
                for item in releases
                if (
                    item.get("album_format")
                    or ""
                ).casefold() == wanted
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
            release_date = release.get("year") or "Brak daty"
            user_score = release.get("user_score") or "NR"

            # Dokładną datę i aktualny User Score bierzemy ze strony wydania.
            try:
                details = await asyncio.to_thread(
                    get_album_details,
                    release["url"],
                )

                release_date = (
                    details.get("release_date")
                    or release_date
                )

                user_score = (
                    details.get("user_score")
                    or user_score
                )

            except AOTYRateLimit:
                # Zostają dane z dyskografii; nie kasujemy całej komendy.
                pass
            except Exception:
                pass

            release_format = (
                release.get("album_format")
                or "?"
            )

            lines.append(
                f"• **[{release['title']}]({release['url']})**"
                f" — {release_date} · {release_format} — ⭐ **{user_score}**"
            )

            await asyncio.sleep(0.12)

        embed = discord.Embed(
            title=discography["artist"],
            url=discography["url"],
            description="\n".join(lines),
        )

        if discography.get("image"):
            embed.set_thumbnail(
                url=discography["image"],
            )

        if len(releases) > len(shown):
            footer = (
                f"{selected_label} • pokazano {len(shown)} "
                f"z {len(releases)} wydań."
            )
        else:
            footer = (
                f"{selected_label} • {len(shown)} wydań."
            )

        embed.set_footer(
            text=footer
        )

        await interaction.followup.send(
            embed=embed,
        )
