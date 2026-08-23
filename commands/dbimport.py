"""Bulk import of CSV bundles made by the local AOTY HTML exporter."""

from __future__ import annotations

import asyncio
import csv
import io
import re
import zipfile
from collections import defaultdict
from pathlib import PurePosixPath

import discord

from database import DB
from rating_import import RatingImportError, parse_aoty_ratings_csv
from settings import GUILD_ID, KOTONE_USERS, is_operator_discord_id


MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_EXPANDED_BYTES = 250 * 1024 * 1024
MAX_CSV_FILES = 200


class BundleImportError(ValueError):
    """The uploaded CSV/ZIP cannot be safely processed."""


def _csv_rows(payload: bytes, name: str) -> list[dict[str, str]]:
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise BundleImportError(f"{name}: plik nie jest UTF-8 CSV") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if not reader.fieldnames:
        raise BundleImportError(f"{name}: brak nagłówków CSV")
    return [dict(row) for row in reader]


def _read_bundle(filename: str, payload: bytes) -> dict[str, bytes]:
    """Read CSV files from memory; do not extract untrusted ZIP paths."""

    if len(payload) > MAX_ARCHIVE_BYTES:
        raise BundleImportError("plik przekracza limit 100 MB")
    name = str(filename or "").casefold()
    if name.endswith(".csv"):
        return {PurePosixPath(filename).name: payload}
    if not name.endswith(".zip"):
        raise BundleImportError("załącz ZIP z folderu GOTOWE CSV albo pojedynczy CSV")
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            entries = [item for item in archive.infolist() if not item.is_dir()]
            csv_entries = [
                item for item in entries
                if item.filename.casefold().endswith(".csv")
                and not PurePosixPath(item.filename).name.startswith(".")
            ]
            if not csv_entries:
                raise BundleImportError("ZIP nie zawiera plików CSV")
            if len(csv_entries) > MAX_CSV_FILES:
                raise BundleImportError("ZIP zawiera zbyt wiele plików CSV")
            total_size = sum(item.file_size for item in csv_entries)
            if total_size > MAX_EXPANDED_BYTES:
                raise BundleImportError("CSV po rozpakowaniu przekraczają limit 250 MB")
            result: dict[str, bytes] = {}
            for item in csv_entries:
                safe_name = PurePosixPath(item.filename).name
                if safe_name in result:
                    raise BundleImportError(f"ZIP zawiera dwa pliki o nazwie {safe_name}")
                result[safe_name] = archive.read(item)
            return result
    except zipfile.BadZipFile as exc:
        raise BundleImportError("plik ZIP jest uszkodzony") from exc


def _target_profile_from_filename(filename: str) -> tuple[str | None, str | None]:
    match = re.fullmatch(r"profile-(.+?)-ratings\.csv", filename, re.I)
    if not match:
        return None, "niepoprawna nazwa pliku profilu"
    wanted = match.group(1).strip().casefold()
    for key, profile in KOTONE_USERS.items():
        aoty_username = str(profile.get("aoty_username") or "").strip()
        if wanted in {str(key).casefold(), aoty_username.casefold()}:
            canonical = DB.canonical_username(aoty_username)
            return canonical, None
    return None, f"profil {match.group(1)!r} nie jest użytkownikiem Kotone"


def _value(row: dict[str, str], key: str) -> str:
    return str(row.get(key) or "").strip()


def _list_value(row: dict[str, str], key: str) -> list[str]:
    return [item.strip() for item in re.split(r"[,;|]", _value(row, key)) if item.strip()]


def _metadata_payload(row: dict[str, str]) -> dict:
    """Translate one local album/artist CSV row into non-destructive details."""

    release_date = _value(row, "Release Date") or _value(row, "Year")
    payload = {
        "artist": _value(row, "Artist") or None,
        "artist_url": _value(row, "Artist URL") or None,
        "album": _value(row, "Album") or None,
        "url": _value(row, "Album URL") or None,
        "cover": _value(row, "Cover URL") or None,
        "release_date": release_date or None,
        "album_format": _value(row, "Format") or None,
        "duration": _value(row, "Duration") or None,
        "label": _value(row, "Label") or None,
        "labels": _list_value(row, "Label"),
        "genres": _list_value(row, "Genres"),
        "secondary_genres": _list_value(row, "Secondary Genres"),
        "vibes": _list_value(row, "Vibes"),
        "user_score": _value(row, "AOTY User Score") or None,
        "ratings_count": re.sub(r"[^0-9]", "", _value(row, "AOTY Ratings")) or None,
        "critic_score": _value(row, "Critic Score") or None,
        "critic_reviews_count": re.sub(r"[^0-9]", "", _value(row, "Critic Reviews")) or None,
        "year_ranking_text": _value(row, "Year Ratings") or None,
        "all_time_ranking": _value(row, "All Time Ratings") or None,
        "must_hear_kind": _value(row, "Must Hear").casefold() or None,
        # Saved HTML is explicitly selected and provided by an operator.
        # Present it as manual Kotone data even when its contents originated
        # from an AOTY page.
        "source": "manual",
    }
    payload = {key: value for key, value in payload.items() if value not in (None, "", [])}
    payload["_section_complete"] = {
        "score": any(key in payload for key in ("user_score", "ratings_count", "critic_score", "critic_reviews_count")),
        "release_date": bool(release_date),
        "format": "album_format" in payload,
        "duration": "duration" in payload,
        "labels": bool(payload.get("label") or payload.get("labels")),
        "genres": bool(payload.get("genres") or payload.get("secondary_genres")),
        "vibes": bool(payload.get("vibes")),
        "ranking": bool(payload.get("year_ranking_text") or payload.get("all_time_ranking")),
        "tracklist": False,
    }
    return payload


def _import_metadata(rows: list[dict[str, str]]) -> tuple[int, int]:
    saved = 0
    skipped = 0
    for row in rows:
        album_id = _value(row, "Album ID")
        if not album_id.isdecimal():
            skipped += 1
            continue
        if DB.save_release_details(
            album_id,
            _metadata_payload(row),
            allow_unscoped_manual=True,
        ):
            saved += 1
        else:
            skipped += 1
    return saved, skipped


def _import_tracks(rows: list[dict[str, str]]) -> tuple[int, int]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        album_id = _value(row, "Album ID")
        title = _value(row, "Title")
        if not album_id.isdecimal() or not title:
            continue
        number = _value(row, "Number")
        grouped[album_id].append({
            "number": int(number) if number.isdecimal() else None,
            "disc": _value(row, "Disc") or None,
            "title": title,
            "duration": _value(row, "Duration") or None,
            "user_score": _value(row, "AOTY Score") or None,
            "url": _value(row, "URL") or None,
        })
    saved = 0
    skipped = 0
    for album_id, tracks in grouped.items():
        if DB.save_release_details(
            album_id,
            {
                "source": "manual",
                "tracklist": tracks,
                "_section_complete": {"tracklist": True},
            },
            allow_unscoped_manual=True,
        ):
            saved += len(tracks)
        else:
            skipped += len(tracks)
    return saved, skipped


def _import_artists(rows: list[dict[str, str]]) -> tuple[int, int]:
    """Persist aliases and compact discography rows only when in Kotone scope."""

    aliases_saved = 0
    releases_saved = 0
    by_artist: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        artist = _value(row, "Artist")
        if artist:
            by_artist[artist].extend(_list_value(row, "Aliases"))
        album_id = _value(row, "Album ID")
        if album_id.isdecimal() and DB.save_release_details(
            album_id,
            _metadata_payload(row),
            allow_unscoped_manual=True,
        ):
            releases_saved += 1
    for artist, aliases in by_artist.items():
        if DB.save_artist_aliases(
            artist,
            aliases,
            source="manual",
            allow_unscoped_manual=True,
        ):
            aliases_saved += 1
    return releases_saved, aliases_saved


def setup_dbimport_command(tree: discord.app_commands.CommandTree) -> None:
    @tree.command(
        name="dbimport",
        description="Masowo importuje lokalne CSV AOTY do SQLite.",
    )
    @discord.app_commands.describe(file="ZIP z GOTOWE CSV albo jeden wygenerowany CSV")
    async def dbimport_command(
        interaction: discord.Interaction,
        file: discord.Attachment,
    ) -> None:
        if interaction.guild_id != GUILD_ID:
            await interaction.response.send_message("Ta komenda działa tylko na serwerze Kotone.", ephemeral=True)
            return
        if not is_operator_discord_id(getattr(interaction.user, "id", None)):
            await interaction.response.send_message("Nie masz uprawnień do `/dbimport`.", ephemeral=True)
            return
        if int(file.size or 0) > MAX_ARCHIVE_BYTES:
            await interaction.response.send_message("Plik przekracza limit 100 MB.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            bundle = await asyncio.to_thread(
                _read_bundle,
                str(file.filename or ""),
                await file.read(),
            )
            await asyncio.to_thread(DB.backup_if_due, force=True)
            profile_results: list[tuple[str, dict]] = []
            skipped: list[str] = []

            # Profile ratings go first.  Album pages explicitly packed by an
            # operator may also create their own release cache entries.
            for name, payload in bundle.items():
                if not re.fullmatch(r"profile-.+?-ratings\.csv", name, re.I):
                    continue
                username, reason = _target_profile_from_filename(name)
                if username is None:
                    skipped.append(f"{name}: {reason}")
                    continue
                parsed = await asyncio.to_thread(parse_aoty_ratings_csv, payload)
                result = await asyncio.to_thread(DB.import_official_ratings, username, parsed["rows"])
                profile_results.append((username, result))

            metadata_rows: list[dict[str, str]] = []
            track_rows: list[dict[str, str]] = []
            artist_rows: list[dict[str, str]] = []
            for name, payload in bundle.items():
                lowered = name.casefold()
                if lowered == "album-metadata.csv":
                    metadata_rows.extend(await asyncio.to_thread(_csv_rows, payload, name))
                elif lowered == "album-tracklist.csv":
                    track_rows.extend(await asyncio.to_thread(_csv_rows, payload, name))
                elif lowered == "artist-discography.csv":
                    artist_rows.extend(await asyncio.to_thread(_csv_rows, payload, name))
                elif not re.fullmatch(r"profile-.+?-ratings\.csv", name, re.I):
                    skipped.append(f"{name}: nierozpoznany typ CSV")

            metadata_saved, metadata_skipped = await asyncio.to_thread(_import_metadata, metadata_rows)
            tracks_saved, tracks_skipped = await asyncio.to_thread(_import_tracks, track_rows)
            artist_releases, artist_aliases = await asyncio.to_thread(_import_artists, artist_rows)
        except (BundleImportError, RatingImportError, ValueError) as exc:
            await interaction.followup.send(f"❌ `/dbimport` zatrzymany: {exc}", ephemeral=True)
            return
        except Exception as exc:
            await interaction.followup.send(
                f"❌ `/dbimport` nie powiódł się: `{type(exc).__name__}: {exc}`",
                ephemeral=True,
            )
            return

        lines = ["✅ **/dbimport zakończony**"]
        for username, result in profile_results:
            lines.append(
                f"• {username}: {result['added']} nowych · {result['updated']} zmian · "
                f"{result['queued_notifications']} powiadomień"
            )
        if metadata_rows:
            lines.append(f"• albumy: {metadata_saved} zapisanych · {metadata_skipped} pominiętych")
        if track_rows:
            lines.append(f"• tracklista: {tracks_saved} utworów · {tracks_skipped} pominiętych")
        if artist_rows:
            lines.append(f"• artyści: {artist_releases} wydań · {artist_aliases} aliasów")
        if skipped:
            lines.append("• pominięte: " + "; ".join(skipped[:3]))
        if len(lines) == 1:
            lines.append("• Nie znaleziono rozpoznawalnych CSV.")
        await interaction.followup.send("\n".join(lines), ephemeral=True)
