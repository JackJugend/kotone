"""Strict, offline parsers for common public Last.fm scrobble CSV exports."""

from __future__ import annotations

import csv
import io
from itertools import chain
from datetime import datetime, timezone

from rating_import import normalized_text


class LastFMImportError(ValueError):
    """The attachment is not a recognised Last.fm listening-history CSV."""


def _epoch(value: object) -> int:
    text = str(value or "").strip()
    if text.isdigit():
        result = int(text)
        # Some exporters write milliseconds while Last.fm's API uses seconds.
        return result // 1000 if result >= 10_000_000_000 else result
    for pattern in (
        "%d %b %Y, %H:%M",
        "%d %b %Y %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S%z",
    ):
        try:
            parsed = datetime.strptime(text, pattern)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    raise LastFMImportError("nierozpoznana data odsłuchu")


def _text(row: dict[str, str], *names: str) -> str | None:
    for name in names:
        value = str(row.get(name) or "").strip()
        if value:
            return value
    return None


def parse_lastfm_scrobbles_csv(payload: bytes) -> dict:
    """Parse either the MBID export or a legacy four-column Last.fm CSV.

    Supported layouts are:
    ``uts,utc_time,artist,artist_mbid,album,album_mbid,track,track_mbid``
    and the headerless ``artist,album,track,"19 Aug 2026, 20:48"`` export.
    Times are normalised to UTC epoch seconds; names are preserved verbatim.
    """

    if not payload:
        raise LastFMImportError("plik jest pusty")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise LastFMImportError("plik CSV musi być zapisany jako UTF-8") from None

    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        first_row = next(reader)
    except StopIteration:
        raise LastFMImportError("plik jest pusty")
    first = [str(value or "").strip().casefold() for value in first_row]
    header_mode = "uts" in first or "utc_time" in first or "track_mbid" in first
    format_name = "lastfm-legacy"
    if header_mode:
        headers = [str(value or "").strip().casefold() for value in first_row]
        if not {"artist", "track"}.issubset(headers) or not (
            "uts" in headers or "utc_time" in headers
        ):
            raise LastFMImportError("brak kolumn artist, track lub daty Last.fm")
        format_name = "lastfm-mbid" if "track_mbid" in headers else "lastfm-header"
        raw_rows = (
            (
                row_index,
                {
                    header: str(values[index]).strip() if index < len(values) else ""
                    for index, header in enumerate(headers)
                },
            )
            for row_index, values in enumerate(reader, start=2)
        )
    else:
        raw_rows = (
            (
                row_index,
                (
                    {
                        "artist": str(values[0]).strip(),
                        "album": str(values[1]).strip(),
                        "track": str(values[2]).strip(),
                        "utc_time": str(values[3]).strip(),
                    }
                    if len(values) >= 4
                    else {"_invalid": "za mało kolumn"}
                ),
            )
            for row_index, values in enumerate(chain([first_row], reader), start=1)
        )

    tracks: list[dict] = []
    rejected: list[dict] = []
    seen: set[tuple] = set()
    duplicates = 0
    for index, raw in raw_rows:
        try:
            if raw.get("_invalid"):
                raise LastFMImportError(raw["_invalid"])
            artist = _text(raw, "artist")
            album = _text(raw, "album")
            track = _text(raw, "track", "name")
            if not artist or not track:
                raise LastFMImportError("brak artysty lub utworu")
            played_at = _epoch(_text(raw, "uts", "utc_time", "date"))
            artist_mbid = _text(raw, "artist_mbid")
            album_mbid = _text(raw, "album_mbid")
            track_mbid = _text(raw, "track_mbid")
            # Exact external IDs take precedence. Without them we deduplicate
            # only an exact normalised listening event; never fuzzy-match music.
            key = (
                (played_at, "mbid", track_mbid.casefold())
                if track_mbid
                else (
                    played_at,
                    "text",
                    normalized_text(artist),
                    normalized_text(album),
                    normalized_text(track),
                )
            )
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
            tracks.append(
                {
                    "played_at": played_at,
                    "artist": artist,
                    "album": album,
                    "track": track,
                    "artist_mbid": artist_mbid,
                    "album_mbid": album_mbid,
                    "track_mbid": track_mbid,
                }
            )
        except LastFMImportError as exc:
            rejected.append({"source_row": index, "reason": str(exc)})

    if not tracks:
        raise LastFMImportError("plik nie zawiera poprawnych scrobbli")
    return {
        "format": format_name,
        "tracks": tracks,
        "rejected": rejected,
        "duplicates": duplicates,
    }
