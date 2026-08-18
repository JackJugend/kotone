"""Polite MusicBrainz fallback for missing public release metadata.

This module is deliberately separate from the AOTY transport.  It has its own
one-request-per-second gate, a meaningful User-Agent and no polling.  It is
called only by Kotone's low-priority background worker after an AOTY failure,
then its result may fill only missing sections in the SQLite release cache.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any

import requests

from settings import (
    MUSICBRAINZ_MIN_REQUEST_INTERVAL,
    MUSICBRAINZ_OUTAGE_COOLDOWN,
    MUSICBRAINZ_REQUEST_TIMEOUT,
)


BASE_URL = "https://musicbrainz.org/ws/2"
USER_AGENT = "Kotone/1.0 (https://github.com/JackJugend/kotone)"


class MusicBrainzUnavailable(RuntimeError):
    """MusicBrainz could not provide a safe fallback result."""

    def __init__(self, message: str, *, retry_after: float = 0.0):
        super().__init__(message)
        self.retry_after = max(0.0, float(retry_after))


def _normalized(value: object) -> str:
    return re.sub(r"[^\w]+", "", str(value or "").casefold())


def _artist_name(credit: object) -> str:
    pieces: list[str] = []
    for item in credit or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("artist", {}).get("name") or "").strip()
        if name:
            pieces.append(name + str(item.get("joinphrase") or ""))
    return "".join(pieces).strip()


def _duration(milliseconds: object) -> str | None:
    try:
        seconds = max(0, int(milliseconds) // 1000)
    except (TypeError, ValueError):
        return None
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def _release_format(release: dict, requested_format: object) -> str | None:
    requested = str(requested_format or "").strip()
    if requested:
        return requested
    primary = str((release.get("release-group") or {}).get("primary-type") or "").strip()
    return {
        "album": "LP",
        "ep": "EP",
        "single": "Single",
        "broadcast": "Live",
    }.get(primary.casefold())


def _names(items: object) -> list[str]:
    names: list[tuple[int, str]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if name:
            try:
                count = int(item.get("count") or 0)
            except (TypeError, ValueError):
                count = 0
            names.append((count, name))
    return [name for _count, name in sorted(names, reverse=True)[:12]]


def _pick_exact_release(candidates: object, artist: str, album: str) -> dict | None:
    expected_artist = _normalized(artist)
    expected_album = _normalized(album)
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        if _normalized(candidate.get("title")) != expected_album:
            continue
        found_artist = _normalized(_artist_name(candidate.get("artist-credit")))
        if found_artist == expected_artist:
            return candidate
    return None


def release_to_details(release: dict, *, requested_format: object = None) -> dict:
    """Convert one MusicBrainz release document to Kotone's release cache."""

    group = release.get("release-group") or {}
    release_date = str(
        release.get("date") or group.get("first-release-date") or ""
    ).strip()
    tracks: list[dict] = []
    sequence = 0
    media = release.get("media") or []
    for disc_index, medium in enumerate(media, start=1):
        if not isinstance(medium, dict):
            continue
        disc = medium.get("position") or disc_index
        for track in medium.get("tracks") or []:
            if not isinstance(track, dict):
                continue
            title = str(track.get("title") or track.get("recording", {}).get("title") or "").strip()
            if not title:
                continue
            sequence += 1
            tracks.append(
                {
                    "number": sequence,
                    "title": title,
                    "duration": _duration(track.get("length")),
                    "disc": disc,
                }
            )

    labels = []
    for label_info in release.get("label-info") or []:
        if not isinstance(label_info, dict):
            continue
        label = str((label_info.get("label") or {}).get("name") or "").strip()
        if label:
            labels.append(label)

    genres = _names(release.get("genres") or group.get("genres"))
    if not genres:
        genres = _names(release.get("tags") or group.get("tags"))
    release_id = str(release.get("id") or "").strip()
    group_id = str(group.get("id") or "").strip()
    cover_id = group_id or release_id
    return {
        "artist": _artist_name(release.get("artist-credit")) or None,
        "album": str(release.get("title") or "").strip() or None,
        # Preserve the original AOTY URL on an existing rating card.  The
        # MusicBrainz release page remains useful as an internal provenance
        # field without replacing that link in Discord.
        "musicbrainz_url": (
            f"https://musicbrainz.org/release/{release_id}" if release_id else None
        ),
        "cover": (
            f"https://coverartarchive.org/release-group/{cover_id}/front-250"
            if cover_id
            else None
        ),
        "release_date": release_date or None,
        "year": release_date[:4] if len(release_date) >= 4 else None,
        "album_format": _release_format(release, requested_format),
        "label": labels[0] if labels else None,
        "labels": labels,
        "genres": genres,
        "tracklist": tracks,
        "_section_complete": {
            "score": False,
            "release_date": bool(release_date),
            "format": bool(_release_format(release, requested_format)),
            "labels": bool(labels),
            "genres": bool(genres),
            "vibes": False,
            "ranking": False,
            "tracklist": bool(tracks),
        },
        "source": "musicbrainz",
    }


class MusicBrainzClient:
    """Minimal official Web Service client with a shared polite rate gate."""

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        self._lock = threading.Lock()
        self._next_request_at = 0.0

    def _json(self, path: str, *, params: dict[str, str]) -> dict:
        with self._lock:
            remaining = self._next_request_at - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            try:
                response = self.session.get(
                    f"{BASE_URL}{path}",
                    params=params,
                    timeout=MUSICBRAINZ_REQUEST_TIMEOUT,
                )
                if response.status_code in {429, 502, 503, 504}:
                    try:
                        retry_after = float(response.headers.get("Retry-After") or 0)
                    except (TypeError, ValueError):
                        retry_after = 0.0
                    raise MusicBrainzUnavailable(
                        f"HTTP {response.status_code}: MusicBrainz temporarily unavailable",
                        retry_after=max(retry_after, MUSICBRAINZ_OUTAGE_COOLDOWN),
                    )
                response.raise_for_status()
                payload = response.json()
            except MusicBrainzUnavailable:
                raise
            except (requests.RequestException, ValueError) as exc:
                raise MusicBrainzUnavailable(
                    str(exc),
                    retry_after=MUSICBRAINZ_OUTAGE_COOLDOWN,
                ) from exc
            finally:
                self._next_request_at = time.monotonic() + MUSICBRAINZ_MIN_REQUEST_INTERVAL
        if not isinstance(payload, dict):
            raise MusicBrainzUnavailable("MusicBrainz returned an invalid JSON document")
        return payload

    def lookup_release(
        self,
        artist: object,
        album: object,
        *,
        requested_format: object = None,
    ) -> dict | None:
        artist_text = str(artist or "").strip()
        album_text = str(album or "").strip()
        if not artist_text or not album_text:
            return None
        query = f'release:"{album_text}" AND artist:"{artist_text}"'
        search = self._json(
            "/release/",
            params={"query": query, "fmt": "json", "limit": "5"},
        )
        candidate = _pick_exact_release(search.get("releases"), artist_text, album_text)
        if candidate is None or not candidate.get("id"):
            return None
        release = self._json(
            f"/release/{candidate['id']}",
            params={
                "fmt": "json",
                "inc": "artists+recordings+release-groups+labels+genres+tags",
            },
        )
        if (
            _normalized(release.get("title")) != _normalized(album_text)
            or _normalized(_artist_name(release.get("artist-credit"))) != _normalized(artist_text)
        ):
            return None
        return release_to_details(release, requested_format=requested_format)


MUSICBRAINZ = MusicBrainzClient()
