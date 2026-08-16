import asyncio
import discord
import requests


MAX_ARTIST_RELEASES = 18


def setup_artist_command(
    tree,
    search_aoty_artists,
    resolve_artist,
    get_artist_releases,
    get_album_details,
    AOTYRateLimit,
):
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
    )
    @discord.app_commands.autocomplete(
        artist=artist_autocomplete,
    )
    async def artist_command(
        interaction: discord.Interaction,
        artist: str,
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

        # /artist ma być listą albumów, więc ukrywamy single i video.
        releases = [
            item
            for item in discography.get("releases", [])
            if (item.get("album_format") or "").casefold()
            not in {"single", "music video", "video"}
        ]

        if not releases:
            await interaction.followup.send(
                f"❌ Nie znaleziono wydań artysty **{discography['artist']}**."
            )
            return

        shown = releases[:MAX_ARTIST_RELEASES]
        lines = []

        for release in shown:
            release_date = release.get("year") or "Brak daty"
            user_score = release.get("user_score") or "NR"

            # Dokładną datę bierzemy ze strony albumu. Jeśli AOTY jej nie ma,
            # zostaje rok ze strony artysty.
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
                # Nie kasujemy całej komendy, jeśli AOTY ograniczy kolejne
                # requesty podczas wzbogacania listy.
                pass
            except Exception:
                pass

            release_format = release.get("album_format")
            format_text = (
                f" · {release_format}"
                if release_format
                else ""
            )

            lines.append(
                f"• **[{release['title']}]({release['url']})**"
                f" — {release_date}{format_text} — ⭐ **{user_score}**"
            )

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
            embed.set_footer(
                text=(
                    f"Pokazano {len(shown)} z {len(releases)} wydań "
                    "(single są pomijane)."
                )
            )
        else:
            embed.set_footer(
                text="Single są pomijane."
            )

        await interaction.followup.send(
            embed=embed,
        )
