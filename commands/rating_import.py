"""Private manual import of official AOTY ratings CSV attachments."""

from __future__ import annotations

import asyncio
import io

import discord

from database import DB
from lastfm_archive import LASTFM_ARCHIVE
from rating_import import (
    RatingImportError,
    parse_aoty_ratings_csv,
    unmatched_report_csv,
)
from settings import (
    GUILD_ID,
    IMPORT_USERS_BY_DISCORD_ID,
    KOTONE_USERS_BY_DISCORD_ID,
)


MAX_CSV_BYTES = 2 * 1024 * 1024


def setup_rating_import_command(tree: discord.app_commands.CommandTree) -> None:
    @tree.command(
        name="import",
        description="Importuje eksport ocen AOTY do kotone.",
    )
    @discord.app_commands.describe(
        source="Źródło importu",
        file="Wymagany wyłącznie dla eksportu AOTY CSV",
    )
    @discord.app_commands.choices(
        source=[
            discord.app_commands.Choice(name="AOTY — plik CSV", value="aoty"),
            discord.app_commands.Choice(name="Last.fm — najnowsze scrobble", value="lastfm"),
        ]
    )
    async def import_command(
        interaction: discord.Interaction,
        source: str = "aoty",
        file: discord.Attachment | None = None,
    ) -> None:
        if interaction.guild_id != GUILD_ID:
            await interaction.response.send_message(
                "Ta komenda działa tylko na skonfigurowanym serwerze.",
                ephemeral=True,
            )
            return

        if source not in {"aoty", "lastfm"}:
            await interaction.response.send_message(
                "Nieznane źródło importu.",
                ephemeral=True,
            )
            return

        discord_user_id = int(getattr(interaction.user, "id", 0) or 0)
        kotone_profile = KOTONE_USERS_BY_DISCORD_ID.get(discord_user_id)
        username = IMPORT_USERS_BY_DISCORD_ID.get(discord_user_id)
        if source == "lastfm" and kotone_profile is None:
            await interaction.response.send_message(
                "Nie masz uprawnień do importu Last.fm.",
                ephemeral=True,
            )
            return
        if source == "aoty" and username is None:
            await interaction.response.send_message(
                "Nie masz uprawnień do `/import`.",
                ephemeral=True,
            )
            return

        if source == "lastfm":
            await interaction.response.defer(ephemeral=True, thinking=True)
            result = await LASTFM_ARCHIVE.import_newest_now(
                str((kotone_profile or {}).get("name") or "")
            )
            if result.get("error"):
                await interaction.followup.send(
                    f"❌ Import Last.fm nie wystartował: {result['error']}",
                    ephemeral=True,
                )
                return
            profile = result["profile"]
            status = "zakończony" if result["complete"] else "kontynuowany w tle"
            await interaction.followup.send(
                f"✅ **Last.fm → {profile['lastfm_username']}**\n"
                f"• zapisano najnowszą stronę: **{result['page']}/{result['total_pages']}**\n"
                f"• nowych scrobbli: **{result['inserted']}**\n"
                f"• łącznie: **{profile.get('total_scrobbles') or '—'}** scrobbli\n"
                f"• starsza historia: **{status}** (najnowsze → najstarsze).",
                ephemeral=True,
            )
            return

        canonical = DB.canonical_username(username)
        if canonical is None:
            await interaction.response.send_message(
                "Przypisany użytkownik AOTY nie znajduje się w kotone.",
                ephemeral=True,
            )
            return

        if file is None:
            await interaction.response.send_message(
                "Dla `source: AOTY — plik CSV` załącz eksport ocen AOTY.",
                ephemeral=True,
            )
            return
        filename = str(file.filename or "")
        if not filename.casefold().endswith(".csv"):
            await interaction.response.send_message(
                "Załącz oficjalny plik `.csv` pobrany z ustawień AOTY.",
                ephemeral=True,
            )
            return
        if int(file.size or 0) > MAX_CSV_BYTES:
            await interaction.response.send_message(
                "Plik jest za duży. Maksymalny rozmiar to 2 MB.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            payload = await file.read()
            if len(payload) > MAX_CSV_BYTES:
                raise RatingImportError("plik przekracza limit 2 MB")
            parsed = await asyncio.to_thread(parse_aoty_ratings_csv, payload)
            await asyncio.to_thread(DB.backup_if_due, force=True)
            result = await asyncio.to_thread(
                DB.import_official_ratings,
                canonical,
                parsed["rows"],
            )
        except RatingImportError as exc:
            await interaction.followup.send(
                f"❌ Niepoprawny eksport AOTY: {exc}",
                ephemeral=True,
            )
            return
        except ValueError as exc:
            await interaction.followup.send(
                f"❌ Import zatrzymany bez zmian: {exc}",
                ephemeral=True,
            )
            return
        except Exception as exc:
            await interaction.followup.send(
                "❌ Import nie powiódł się. Baza pozostała transakcyjna, a "
                f"przed zapisem utworzono backup. `{type(exc).__name__}: {exc}`",
                ephemeral=True,
            )
            return

        unresolved = list(result["unmatched"]) + list(parsed["rejected"])
        message = (
            f"✅ **Import AOTY → {canonical} zakończony**\n"
            f"• poprawne rekordy: **{result['total']}**\n"
            f"• dodane: **{result['added']}**\n"
            f"• zaktualizowane: **{result['updated']}**\n"
            f"• bez zmian: **{result['unchanged']}**\n"
            f"• nowe powiadomienia w kolejce: "
            f"**{result['queued_notifications']}**\n"
            f"• nierozpoznane/błędne: **{len(unresolved)}**\n"
            f"• duplikaty w CSV: **{parsed['duplicates']}**\n\n"
            "Istniejące reviews, likes, Track Ratings i metadane nie zostały "
            "usunięte. Brakujące pozycje nie zostały oznaczone jako usunięte. "
            "Nowe rekordy ocenione po ostatnim potwierdzonym powiadomieniu "
            "oczekują na pojedynczą wysyłkę monitora."
        )
        if unresolved:
            report = discord.File(
                io.BytesIO(unmatched_report_csv(unresolved)),
                filename=f"import-{canonical}-nierozpoznane.csv",
            )
            await interaction.followup.send(
                message,
                file=report,
                ephemeral=True,
            )
        else:
            await interaction.followup.send(message, ephemeral=True)
