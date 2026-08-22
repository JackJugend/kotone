"""Private manual import of official AOTY ratings CSV attachments."""

from __future__ import annotations

import asyncio
import io

import discord

from database import DB
from import_payload import ImportPayloadError, extract_csv_payload
from lastfm_globals import LASTFM_DB
from lastfm_import import LastFMImportError, parse_lastfm_scrobbles_csv
from rating_import import (
    RatingImportError,
    parse_aoty_ratings_csv,
    unmatched_report_csv,
)
from settings import (
    GUILD_ID,
    IMPORT_USERS_BY_DISCORD_ID,
    KOTONE_USERS,
    KOTONE_USERS_BY_DISCORD_ID,
    is_operator_discord_id,
)


# Discord still applies the guild's upload cap before the command sees a
# file, but Kotone itself accepts a full 100 MiB export once Discord passes it.
MAX_CSV_BYTES = 100 * 1024 * 1024
MAX_LASTFM_CSV_BYTES = MAX_CSV_BYTES


async def _kotone_user_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[discord.app_commands.Choice[str]]:
    """Offer every configured Kotone profile only to import operators."""

    if not is_operator_discord_id(getattr(interaction.user, "id", None)):
        return []
    needle = str(current or "").casefold()
    choices: list[discord.app_commands.Choice[str]] = []
    for key, profile in KOTONE_USERS.items():
        labels = [str(key)]
        if profile.get("aoty_username"):
            labels.append(f"AOTY: {profile['aoty_username']}")
        if profile.get("lastfm_username"):
            labels.append(f"Last.fm: {profile['lastfm_username']}")
        label = " · ".join(labels)
        if needle and needle not in label.casefold():
            continue
        choices.append(discord.app_commands.Choice(name=label[:100], value=str(key)))
    return choices[:25]


def setup_rating_import_command(tree: discord.app_commands.CommandTree) -> None:
    @tree.command(
        name="import",
        description="Importuje dane AOTY lub Last.fm do kotone.",
    )
    @discord.app_commands.describe(
        source="Źródło importu",
        file="Plik .csv, .csv.gz lub .zip z jednym CSV",
    )
    @discord.app_commands.autocomplete(username=_kotone_user_autocomplete)
    @discord.app_commands.choices(
        source=[
            discord.app_commands.Choice(name="AOTY — plik CSV", value="aoty"),
            discord.app_commands.Choice(name="Last.fm — plik CSV", value="lastfm"),
        ]
    )
    async def import_command(
        interaction: discord.Interaction,
        source: str = "aoty",
        file: discord.Attachment | None = None,
        username: str | None = None,
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
        actor_profile = KOTONE_USERS_BY_DISCORD_ID.get(discord_user_id)
        is_operator = is_operator_discord_id(discord_user_id)
        requested_key = str(username or "").strip().casefold()
        actor_key = str((actor_profile or {}).get("name") or "").casefold()
        if requested_key and not is_operator and requested_key != actor_key:
            await interaction.response.send_message(
                "Tylko operator może importować dane innego użytkownika kotone.",
                ephemeral=True,
            )
            return
        profile_key = requested_key or actor_key
        kotone_profile = KOTONE_USERS.get(profile_key)
        if kotone_profile is None:
            await interaction.response.send_message(
                "Wybierz użytkownika kotone.",
                ephemeral=True,
            )
            return

        actor_aoty_username = IMPORT_USERS_BY_DISCORD_ID.get(discord_user_id)
        target_aoty_username = str(kotone_profile.get("aoty_username") or "").strip()
        if source == "lastfm" and not kotone_profile.get("lastfm_username"):
            await interaction.response.send_message(
                "Wybrany użytkownik nie ma ustawionego konta Last.fm.",
                ephemeral=True,
            )
            return
        if source == "aoty" and not target_aoty_username:
            await interaction.response.send_message(
                "Wybrany użytkownik nie ma ustawionego konta AOTY.",
                ephemeral=True,
            )
            return
        if source == "aoty" and not is_operator and actor_aoty_username is None:
            await interaction.response.send_message(
                "Nie masz uprawnień do `/import`.",
                ephemeral=True,
            )
            return

        if source == "lastfm":
            if file is None:
                await interaction.response.send_message(
                    "Dla importu Last.fm załącz `.csv`, `.csv.gz` albo `.zip` z CSV. Eksport możesz "
                    "pobrać z <https://lastfm.ghan.nl/export/> z ustawieniem scrobbles.",
                    ephemeral=True,
                )
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            filename = str(file.filename or "")
            if int(file.size or 0) > MAX_LASTFM_CSV_BYTES:
                await interaction.followup.send(
                    "Plik Last.fm jest za duży. Maksymalny rozmiar to 100 MB.",
                    ephemeral=True,
                )
                return
            try:
                payload, _container = extract_csv_payload(
                    filename,
                    await file.read(),
                    max_bytes=MAX_LASTFM_CSV_BYTES,
                )
                parsed = await asyncio.to_thread(parse_lastfm_scrobbles_csv, payload)
                # This import is strictly offline: matching MBIDs against the
                # local SQLite cache performs no Last.fm/AOTY request.
                tracks = await asyncio.to_thread(
                    DB.link_lastfm_tracks_to_releases,
                    list(parsed["tracks"]),
                )
                inserted = await asyncio.to_thread(
                    LASTFM_DB.import_tracks,
                    str(kotone_profile.get("name") or ""),
                    tracks,
                )
                # The attachment is an offline history import.  Record only
                # its cursor state, not a source field on every scrobble, so
                # the background worker does not re-download the same past.
                await asyncio.to_thread(
                    LASTFM_DB.mark_imported_complete,
                    str(kotone_profile.get("name") or ""),
                )
                stats = await asyncio.to_thread(
                    LASTFM_DB.archive_statistics,
                    str(kotone_profile.get("name") or ""),
                )
            except (ImportPayloadError, LastFMImportError) as exc:
                await interaction.followup.send(
                    f"❌ Niepoprawny CSV Last.fm: {exc}",
                    ephemeral=True,
                )
                return
            except Exception as exc:
                await interaction.followup.send(
                    f"❌ Import Last.fm nie powiódł się: `{type(exc).__name__}: {exc}`",
                    ephemeral=True,
                )
                return
            await interaction.followup.send(
                f"✅ **Last.fm CSV → {kotone_profile.get('lastfm_username')}**\n"
                f"• dodane scrobble: **{inserted}**\n"
                f"• duplikaty w pliku: **{parsed['duplicates']}**\n"
                f"• odrzucone wiersze: **{len(parsed['rejected'])}**\n"
                f"• archiwum kotone: **{stats['scrobbles']}** scrobbli · "
                f"**{stats['artists']}** wykonawców · **{stats['albums']}** albumów · "
                f"**{stats['tracks']}** utworów",
                ephemeral=True,
            )
            return

        canonical = DB.canonical_username(target_aoty_username)
        if canonical is None:
            await interaction.response.send_message(
                "Przypisany użytkownik AOTY nie znajduje się w kotone.",
                ephemeral=True,
            )
            return

        if file is None:
            await interaction.response.send_message(
                "Nie dodano pliku. Dla `source: AOTY — plik CSV` załącz eksport "
                "ocen AOTY. Jeśli chcesz zaimportować historię **Last.fm**, wybierz "
                "źródło `Last.fm — API lub plik CSV` i dodaj eksport z "
                "<https://lastfm.ghan.nl/export/>.",
                ephemeral=True,
            )
            return
        filename = str(file.filename or "")
        if int(file.size or 0) > MAX_CSV_BYTES:
            await interaction.response.send_message(
                "Plik jest za duży. Maksymalny rozmiar to 100 MB.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            payload, _container = extract_csv_payload(
                filename,
                await file.read(),
                max_bytes=MAX_CSV_BYTES,
            )
            parsed = await asyncio.to_thread(parse_aoty_ratings_csv, payload)
            await asyncio.to_thread(DB.backup_if_due, force=True)
            result = await asyncio.to_thread(
                DB.import_official_ratings,
                canonical,
                parsed["rows"],
            )
        except (ImportPayloadError, RatingImportError) as exc:
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
            f"• duplikaty w CSV: **{parsed['duplicates']}**"
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
