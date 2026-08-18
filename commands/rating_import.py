"""Private manual import of official AOTY ratings CSV attachments."""

from __future__ import annotations

import asyncio
import io

import discord

from database import DB
from rating_import import (
    RatingImportError,
    parse_aoty_ratings_csv,
    unmatched_report_csv,
)
from settings import GUILD_ID


IMPORT_USERS_BY_DISCORD_ID = {
    805601151366070292: "enso",
    463642066401099786: "kulkien",
}
MAX_CSV_BYTES = 2 * 1024 * 1024


def setup_rating_import_command(tree: discord.app_commands.CommandTree) -> None:
    @tree.command(
        name="import",
        description="Importuje Twój oficjalny eksport ocen AOTY do SQLite.",
    )
    @discord.app_commands.describe(
        file="Plik CSV pobrany przez AOTY Settings → Export Ratings",
    )
    async def import_command(
        interaction: discord.Interaction,
        file: discord.Attachment,
    ) -> None:
        if interaction.guild_id != GUILD_ID:
            await interaction.response.send_message(
                "Ta komenda działa tylko na skonfigurowanym serwerze.",
                ephemeral=True,
            )
            return

        discord_user_id = int(getattr(interaction.user, "id", 0) or 0)
        username = IMPORT_USERS_BY_DISCORD_ID.get(discord_user_id)
        if username is None:
            await interaction.response.send_message(
                "Nie masz uprawnień do `/import`.",
                ephemeral=True,
            )
            return
        canonical = DB.canonical_username(username)
        if canonical is None:
            await interaction.response.send_message(
                "Przypisany użytkownik AOTY nie znajduje się w `config.json`.",
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
            f"• nierozpoznane/błędne: **{len(unresolved)}**\n"
            f"• duplikaty w CSV: **{parsed['duplicates']}**\n\n"
            "Istniejące reviews, likes, Track Ratings i metadane nie zostały "
            "usunięte. Brakujące pozycje nie zostały oznaczone jako usunięte."
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
