"""Polite Last.fm Web Services client used only by Kotone's background cache.

Kotone never scrapes Last.fm HTML. Read requests use the documented API and
are skipped entirely until ``LASTFM_API_KEY`` is configured in Railway.
"""

from __future__ import annotations

import threading
import time
import re
from typing import Any

import requests

from settings import (
    LASTFM_API_ENABLED,
    LASTFM_API_KEY,
    LASTFM_MIN_REQUEST_INTERVAL,
    LASTFM_OUTAGE_COOLDOWN,
)


API_URL = "https://ws.audioscrobbler.com/2.0/"
USER_AGENT = "Kotone/1.0 (https://github.com/JackJugend/kotone)"
REQUEST_TIMEOUT = (5, 15)


class LastFMUnavailable(RuntimeError):
    """Last.fm cannot currently provide a safe API response."""

    def __init__(self, message: str, *, retry_after: float = 0.0):
        super().__init__(message)
        self.retry_after = max(0.0, float(retry_after))


def artist_lookup_candidates(value: object) -> list[str]:
    """Return conservative Last.fm aliases for an AOTY artist credit.

    AOTY often uses a parent-group plus subunit credit (for example
    ``tripleS +26 moon``), while Last.fm scrobbles the same release under the
    parent group. We try the exact credit first and only then its parent.
    """

    exact = " ".join(str(value or "").split())
    if not exact:
        return []
    candidates = [exact]
    parent = re.split(r"\s+\+\s+", exact, maxsplit=1)[0].strip()
    if parent and parent.casefold() != exact.casefold():
        candidates.append(parent)
    return candidates


def _image_url(images: object) -> str | None:
    """Choose the largest non-placeholder image returned by Last.fm."""

    preferred: str | None = None
    for image in images or []:
        if not isinstance(image, dict):
            continue
        url = str(image.get("#text") or image.get("url") or "").strip()
        if not url or "2a96cbd8b46e442fc41c2b86b821562f" in url.casefold():
            continue
        if url.startswith("https://"):
            preferred = url
    return preferred


def _duration(milliseconds_or_seconds: object) -> str | None:
    try:
        raw = int(float(milliseconds_or_seconds))
    except (TypeError, ValueError):
        return None
    seconds = raw // 1000 if raw > 86_400 else raw
    if seconds <= 0:
        return None
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


class LastFMClient:
    """One-request-at-a-time Last.fm client with no silent HTML fallback."""

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        self._lock = threading.Lock()
        self._next_request_at = 0.0
        self._blocked_until = 0.0
        self._last_error: str | None = None

    def status(self) -> dict[str, float | bool]:
        """Return the state of the shared conservative rate gate."""

        with self._lock:
            remaining = max(0.0, self._blocked_until - time.monotonic())
        return {
            "configured": LASTFM_API_ENABLED,
            "blocked": remaining > 0,
            "blocked_seconds": round(remaining, 1),
            "last_error": self._last_error,
        }

    def _json(self, method: str, **params: object) -> dict[str, Any]:
        if not LASTFM_API_ENABLED:
            raise LastFMUnavailable("Brak LASTFM_API_KEY; Last.fm API jest wyłączone.")
        with self._lock:
            blocked_seconds = self._blocked_until - time.monotonic()
            if blocked_seconds > 0:
                raise LastFMUnavailable(
                    "Last.fm jest chwilowo w globalnym cooldownie.",
                    retry_after=blocked_seconds,
                )
            remaining = self._next_request_at - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            try:
                response = self.session.get(
                    API_URL,
                    params={
                        "method": method,
                        "api_key": LASTFM_API_KEY,
                        "format": "json",
                        **{key: value for key, value in params.items() if value not in (None, "")},
                    },
                    timeout=REQUEST_TIMEOUT,
                )
                if response.status_code in {429, 500, 502, 503, 504}:
                    try:
                        retry_after = float(response.headers.get("Retry-After") or 0)
                    except (TypeError, ValueError):
                        retry_after = 0.0
                    pause = max(retry_after, LASTFM_OUTAGE_COOLDOWN)
                    self._blocked_until = time.monotonic() + pause
                    self._last_error = (
                        f"HTTP {response.status_code}: Last.fm temporary unavailable"
                    )
                    raise LastFMUnavailable(
                        self._last_error,
                        retry_after=pause,
                    )
                if 400 <= response.status_code < 500:
                    # A missing album (404), a malformed query or a bad key
                    # is not a service-wide outage. Let the caller defer only
                    # this item; do not silence all Last.fm enrichment.
                    self._last_error = (
                        f"HTTP {response.status_code}: Last.fm rejected this request"
                    )
                    raise LastFMUnavailable(self._last_error)
                response.raise_for_status()
                payload = response.json()
                if "error" in payload:
                    code = str(payload.get("error") or "")
                    if code in {"26", "29"}:
                        self._blocked_until = time.monotonic() + LASTFM_OUTAGE_COOLDOWN
                        self._last_error = (
                            f"Last.fm API {code}: "
                            f"{payload.get('message') or 'rate limited'}"
                        )
                        raise LastFMUnavailable(
                            self._last_error,
                            retry_after=LASTFM_OUTAGE_COOLDOWN,
                        )
            except (requests.RequestException, ValueError) as exc:
                self._blocked_until = time.monotonic() + LASTFM_OUTAGE_COOLDOWN
                self._last_error = f"{type(exc).__name__}: {exc}"
                raise LastFMUnavailable(
                    self._last_error, retry_after=LASTFM_OUTAGE_COOLDOWN
                ) from exc
            finally:
                self._next_request_at = time.monotonic() + LASTFM_MIN_REQUEST_INTERVAL

        if not isinstance(payload, dict):
            self._last_error = "Last.fm returned invalid JSON"
            raise LastFMUnavailable("Last.fm zwrócił niepoprawny JSON.")
        if "error" in payload:
            self._last_error = (
                f"Last.fm API {payload.get('error')}: "
                f"{payload.get('message') or 'unknown error'}"
            )
            raise LastFMUnavailable(
                self._last_error
            )
        self._last_error = None
        return payload

    def artist_info(self, artist: object, *, username: object = None) -> dict[str, Any] | None:
        name = str(artist or "").strip()
        if not name:
            return None
        payload = self._json("artist.getInfo", artist=name, username=username, autocorrect=1)
        data = payload.get("artist")
        if not isinstance(data, dict):
            return None
        stats = data.get("stats") if isinstance(data.get("stats"), dict) else {}
        tags = (data.get("tags") or {}).get("tag") or []
        if isinstance(tags, dict):
            tags = [tags]
        return {
            "artist": str(data.get("name") or name).strip(),
            # Last.fm exposes MusicBrainz IDs in these fields; do not pretend
            # they are Last.fm identifiers in the durable provenance cache.
            "musicbrainz_artist_id": str(data.get("mbid") or "").strip() or None,
            "lastfm_url": str(data.get("url") or "").strip() or None,
            "image_url": _image_url(data.get("image")),
            "listeners_count": str(stats.get("listeners") or "").strip() or None,
            "playcount": str(stats.get("playcount") or "").strip() or None,
            "user_playcount": str(stats.get("userplaycount") or "").strip() or None,
            "tags": [
                str(tag.get("name") or "").strip()
                for tag in tags
                if isinstance(tag, dict) and str(tag.get("name") or "").strip()
            ],
        }

    def album_info(self, artist: object, album: object, *, username: object = None) -> dict[str, Any] | None:
        artist_name = str(artist or "").strip()
        album_name = str(album or "").strip()
        if not artist_name or not album_name:
            return None
        payload = self._json(
            "album.getInfo", artist=artist_name, album=album_name,
            username=username, autocorrect=1,
        )
        data = payload.get("album")
        if not isinstance(data, dict):
            return None
        tracks: list[dict[str, Any]] = []
        raw_tracks = ((data.get("tracks") or {}).get("track") or [])
        if isinstance(raw_tracks, dict):
            raw_tracks = [raw_tracks]
        for index, track in enumerate(raw_tracks, start=1):
            if not isinstance(track, dict):
                continue
            title = str(track.get("name") or "").strip()
            if title:
                rank = (track.get("@attr") or {}).get("rank")
                try:
                    number = int(rank or index)
                except (TypeError, ValueError):
                    number = index
                tracks.append({
                    "number": number,
                    "title": title,
                    "duration": _duration(track.get("duration")),
                    "lastfm_track_id": str(track.get("mbid") or "").strip() or None,
                    "url": str(track.get("url") or "").strip() or None,
                })
        tags = (data.get("tags") or {}).get("tag") or []
        if isinstance(tags, dict):
            tags = [tags]
        return {
            "artist": str(data.get("artist") or artist_name).strip(),
            "album": str(data.get("name") or album_name).strip(),
            "musicbrainz_release_group_id": str(data.get("mbid") or "").strip() or None,
            "lastfm_url": str(data.get("url") or "").strip() or None,
            "cover": _image_url(data.get("image")),
            "listeners_count": str(data.get("listeners") or "").strip() or None,
            "playcount": str(data.get("playcount") or "").strip() or None,
            "user_playcount": str(data.get("userplaycount") or "").strip() or None,
            "release_date": str(data.get("releasedate") or "").strip() or None,
            "tracklist": tracks,
            "tags": [
                str(tag.get("name") or "").strip()
                for tag in tags
                if isinstance(tag, dict) and str(tag.get("name") or "").strip()
            ],
            "external_metadata": {
                "lastfm_url": str(data.get("url") or "").strip() or None,
                "listeners_count": str(data.get("listeners") or "").strip() or None,
                "playcount": str(data.get("playcount") or "").strip() or None,
                "user_playcount": str(data.get("userplaycount") or "").strip() or None,
                "musicbrainz_release_group_id": str(data.get("mbid") or "").strip() or None,
                "tracks": [
                    {
                        "number": track["number"],
                        "title": track["title"],
                        "duration": track["duration"],
                        "musicbrainz_recording_id": track["lastfm_track_id"],
                    }
                    for track in tracks
                ],
            },
            "_section_complete": {
                "release_date": bool(str(data.get("releasedate") or "").strip()),
                "labels": False,
                "tracklist": bool(tracks),
            },
        }


LASTFM = LastFMClient()


def fetch_artist_image(artist: object, *, session=None) -> str | None:
    """Compatibility wrapper used by the existing artist-image cache."""

    client = LASTFM if session is None else LastFMClient()
    if session is not None:
        client.session = session
    data = client.artist_info(artist)
    return str((data or {}).get("image_url") or "").strip() or None
