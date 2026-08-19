"""Private manual import of official AOTY ratings CSV attachments."""

from __future__ import annotations

import asyncio
import io

import discord

from database import DB
from lastfm_archive import LASTFM_ARCHIVE
from lastfm_database import LASTFM_DB
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


MAX_CSV_BYTES = 2 * 1024 * 1024
MAX_LASTFM_CSV_BYTES = 20 * 1024 * 1024


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
        description="Importuje dane AOTY lub Last.fm do Kotone.",
    )
    @discord.app_commands.describe(
        source="Źródło importu",
        file="Plik CSV AOTY lub Last.fm (opcjonalny dla Last.fm API)",
    )
    @discord.app_commands.autocomplete(username=_kotone_user_autocomplete)
    @discord.app_commands.choices(
        source=[
            discord.app_commands.Choice(name="AOTY — plik CSV", value="aoty"),
            discord.app_commands.Choice(name="Last.fm — API lub plik CSV", value="lastfm"),
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
                "Tylko operator może importować dane innego użytkownika Kotone.",
                ephemeral=True,
            )
            return
        profile_key = requested_key or actor_key
        kotone_profile = KOTONE_USERS.get(profile_key)
        if kotone_profile is None:
            await interaction.response.send_message(
                "Wybierz użytkownika Kotone z config.json.",
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
            await interaction.response.defer(ephemeral=True, thinking=True)
            if file is not None:
                filename = str(file.filename or "")
                if not filename.casefold().endswith(".csv"):
                    await interaction.followup.send(
                        "Załącz plik `.csv` z historią Last.fm.",
                        ephemeral=True,
                    )
                    return
                if int(file.size or 0) > MAX_LASTFM_CSV_BYTES:
                    await interaction.followup.send(
                        "Plik Last.fm jest za duży. Maksymalny rozmiar to 20 MB.",
                        ephemeral=True,
                    )
                    return
                try:
                    payload = await file.read()
                    if len(payload) > MAX_LASTFM_CSV_BYTES:
                        raise LastFMImportError("plik przekracza limit 20 MB")
                    parsed = await asyncio.to_thread(parse_lastfm_scrobbles_csv, payload)
                    tracks = await asyncio.to_thread(
                        DB.link_lastfm_tracks_to_releases,
                        list(parsed["tracks"]),
                    )
                    inserted = await asyncio.to_thread(
                        LASTFM_DB.import_tracks,
                        str(kotone_profile.get("name") or ""),
                        tracks,
                    )
                    stats = await asyncio.to_thread(
                        LASTFM_DB.archive_statistics,
                        str(kotone_profile.get("name") or ""),
                    )
                except LastFMImportError as exc:
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
                    f"• wykryty format: **{parsed['format']}**\n"
                    f"• dodane scrobble: **{inserted}**\n"
                    f"• duplikaty w pliku: **{parsed['duplicates']}**\n"
                    f"• odrzucone wiersze: **{len(parsed['rejected'])}**\n"
                    f"• archiwum Kotone: **{stats['scrobbles']}** scrobbli · "
                    f"**{stats['artists']}** wykonawców · **{stats['albums']}** albumów · "
                    f"**{stats['tracks']}** utworów\n\n"
                    "Daty zapisano w UTC. Powiązania z AOTY są tworzone tylko "
                    "dla dokładnie zgodnych identyfikatorów MusicBrainz.",
                    ephemeral=True,
                )
                return
            result = await LASTFM_ARCHIVE.import_newest_now(
                str(kotone_profile.get("name") or "")
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

        canonical = DB.canonical_username(target_aoty_username)
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
