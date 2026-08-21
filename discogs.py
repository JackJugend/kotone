"""Ostrożny fallback Discogs dla publicznej tracklisty i czasu wydania.

Nie jest to scraper stron Discogs. Moduł korzysta wyłącznie z publicznego API,
ma własny trwały cooldown oraz wolny wspólny limit. Jest uruchamiany przez
worker w tle dopiero, gdy AOTY i MusicBrainz nie dostarczyły brakującej
tracklisty albo długości wydania.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any

import requests

from settings import (
    DISCOGS_MAX_OUTAGE_COOLDOWN,
    DISCOGS_MIN_REQUEST_INTERVAL,
    DISCOGS_OUTAGE_COOLDOWN,
    DISCOGS_REQUEST_TIMEOUT,
    DISCOGS_STATE_FILE,
    DISCOGS_TOKEN,
)


BASE_URL = "https://api.discogs.com"
USER_AGENT = "Kotone/1.0 +https://github.com/JackJugend/kotone"


class DiscogsUnavailable(RuntimeError):
    """Discogs could not safely provide a fallback result."""

    def __init__(self, message: str, *, retry_after: float = 0.0):
        super().__init__(message)
        self.retry_after = max(0.0, float(retry_after))


def _normalized(value: object) -> str:
    return re.sub(r"[^\w]+", "", str(value or "").casefold())


def _duration(seconds: int) -> str | None:
    if seconds <= 0:
        return None
    hours, remaining = divmod(seconds, 3600)
    minutes, remaining = divmod(remaining, 60)
    return f"{hours}:{minutes:02d}:{remaining:02d}" if hours else f"{minutes}:{remaining:02d}"


def _parse_duration(value: object) -> tuple[str | None, int]:
    text = str(value or "").strip()
    match = re.fullmatch(r"(?:(\d+):)?(\d{1,2}):(\d{2})", text)
    if match:
        hours = int(match.group(1) or 0)
        minutes = int(match.group(2))
        seconds = int(match.group(3))
        total = hours * 3600 + minutes * 60 + seconds
        return _duration(total), total
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if match:
        total = int(match.group(1)) * 60 + int(match.group(2))
        return _duration(total), total
    return None, 0


def _artist_name(value: object) -> str:
    names: list[str] = []
    for item in value or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if name:
            names.append(re.sub(r"\s*\(\d+\)$", "", name))
    return ", ".join(names)


def release_to_details(release: dict[str, Any]) -> dict[str, Any] | None:
    """Map one verified Discogs release into Kotone's narrow fallback shape."""

    tracks: list[dict[str, object]] = []
    total_seconds = 0
    sequence = 0
    disc = 1
    for raw in release.get("tracklist") or []:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("type_") or "track").casefold()
        if kind in {"heading", "index"}:
            if kind == "heading" and tracks:
                disc += 1
            continue
        title = str(raw.get("title") or "").strip()
        if not title:
            continue
        sequence += 1
        parsed_duration, seconds = _parse_duration(raw.get("duration"))
        total_seconds += seconds
        tracks.append(
            {
                "number": sequence,
                "title": title,
                "duration": parsed_duration,
                "disc": disc,
            }
        )

    total_duration = _duration(total_seconds)
    release_date = str(release.get("released") or release.get("year") or "").strip()
    if not tracks and not total_duration and not release_date:
        return None
    release_id = str(release.get("id") or "").strip()
    master_id = str(release.get("master_id") or "").strip()
    return {
        "tracklist": tracks,
        "duration": total_duration,
        "release_date": release_date or None,
        "year": release_date[:4] if len(release_date) >= 4 else None,
        "external_metadata": {
            "discogs_release_id": release_id or None,
            "discogs_url": (
                f"https://www.discogs.com/release/{release_id}" if release_id else None
            ),
            "discogs_master_id": master_id or None,
            "discogs_master_url": (
                f"https://www.discogs.com/master/{master_id}" if master_id else None
            ),
        },
        "_section_complete": {
            "duration": bool(total_duration),
            "tracklist": bool(tracks),
            "release_date": bool(release_date),
        },
        "source": "discogs",
    }


class DiscogsClient:
    """Small Discogs API client with one durable provider-wide circuit."""

    def __init__(self, *, token: str | None = None, state_file: str | None = None):
        self.token = str(token if token is not None else DISCOGS_TOKEN).strip()
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        if self.token:
            self.session.headers["Authorization"] = f"Discogs token={self.token}"
        self._lock = threading.Lock()
        self._next_request_at = 0.0
        self._state_file = str(state_file or DISCOGS_STATE_FILE)
        self._blocked_until = 0.0
        self._consecutive_failures = 0
        self._last_error: str | None = None
        self._restore_state()

    def _restore_state(self) -> None:
        try:
            with open(self._state_file, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            blocked_until = float(payload.get("blocked_until") or 0)
            if blocked_until > time.time():
                self._blocked_until = blocked_until
                self._consecutive_failures = max(1, int(payload.get("consecutive_failures") or 1))
                self._last_error = str(payload.get("last_error") or "") or None
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return

    def _persist_state_locked(self) -> None:
        temporary = f"{self._state_file}.tmp"
        try:
            directory = os.path.dirname(self._state_file)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "blocked_until": self._blocked_until,
                        "consecutive_failures": self._consecutive_failures,
                        "last_error": self._last_error,
                    },
                    handle,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            os.replace(temporary, self._state_file)
        except OSError:
            try:
                os.unlink(temporary)
            except OSError:
                pass

    def _record_outage_locked(self, message: str, *, retry_after: float = 0.0) -> float:
        self._consecutive_failures = min(self._consecutive_failures + 1, 8)
        delay = max(float(retry_after or 0), DISCOGS_OUTAGE_COOLDOWN)
        delay = min(DISCOGS_MAX_OUTAGE_COOLDOWN, delay * (2 ** (self._consecutive_failures - 1)))
        self._blocked_until = max(self._blocked_until, time.time() + delay)
        self._last_error = message
        self._persist_state_locked()
        return delay

    def _record_success_locked(self) -> None:
        if self._blocked_until or self._consecutive_failures or self._last_error:
            self._blocked_until = 0.0
            self._consecutive_failures = 0
            self._last_error = None
            self._persist_state_locked()

    def status(self) -> dict[str, object]:
        with self._lock:
            remaining = max(0.0, self._blocked_until - time.time())
            return {
                "configured": bool(self.token),
                "blocked": remaining > 0,
                "blocked_seconds": round(remaining, 1),
                "consecutive_failures": self._consecutive_failures,
                "last_error": self._last_error,
            }

    def _json(self, path: str, *, params: dict[str, str]) -> dict[str, Any]:
        if not self.token:
            raise DiscogsUnavailable("Brak DISCOGS_TOKEN.")
        with self._lock:
            remaining = self._blocked_until - time.time()
            if remaining > 0:
                raise DiscogsUnavailable("Discogs jest chwilowo w globalnym cooldownie.", retry_after=remaining)
            sleep_for = self._next_request_at - time.monotonic()
            if sleep_for > 0:
                time.sleep(sleep_for)
            try:
                response = self.session.get(f"{BASE_URL}{path}", params=params, timeout=DISCOGS_REQUEST_TIMEOUT)
                if response.status_code in {429, 403} or response.status_code >= 500:
                    try:
                        retry_after = float(response.headers.get("Retry-After") or 0)
                    except (TypeError, ValueError):
                        retry_after = 0.0
                    delay = self._record_outage_locked(
                        f"HTTP {response.status_code}: Discogs temporarily unavailable",
                        retry_after=retry_after,
                    )
                    raise DiscogsUnavailable(
                        f"HTTP {response.status_code}: Discogs temporarily unavailable",
                        retry_after=delay,
                    )
                if 400 <= response.status_code < 500:
                    response.raise_for_status()
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    delay = self._record_outage_locked("Discogs returned invalid JSON")
                    raise DiscogsUnavailable("Discogs returned invalid JSON", retry_after=delay)
                self._record_success_locked()
                return payload
            except DiscogsUnavailable:
                raise
            except requests.HTTPError as exc:
                response = getattr(exc, "response", None)
                status = getattr(response, "status_code", None)
                if status is not None and 400 <= status < 500:
                    raise DiscogsUnavailable(str(exc)) from exc
                delay = self._record_outage_locked(str(exc))
                raise DiscogsUnavailable(str(exc), retry_after=delay) from exc
            except (requests.RequestException, ValueError) as exc:
                delay = self._record_outage_locked(str(exc))
                raise DiscogsUnavailable(str(exc), retry_after=delay) from exc
            finally:
                self._next_request_at = time.monotonic() + DISCOGS_MIN_REQUEST_INTERVAL

    def lookup_release(self, artist: object, album: object) -> dict[str, Any] | None:
        """Find an exact release and return only its public tracks/duration."""

        artist_text = str(artist or "").strip()
        album_text = str(album or "").strip()
        if not artist_text or not album_text:
            return None
        search = self._json(
            "/database/search",
            params={
                "artist": artist_text,
                "release_title": album_text,
                "type": "release",
                "per_page": "5",
            },
        )
        expected_artist = _normalized(artist_text)
        expected_album = _normalized(album_text)
        candidate_id = ""
        for candidate in search.get("results") or []:
            if not isinstance(candidate, dict) or not candidate.get("id"):
                continue
            title = str(candidate.get("title") or "")
            candidate_artist, separator, candidate_album = title.partition(" - ")
            if not separator:
                continue
            if _normalized(candidate_artist) == expected_artist and _normalized(candidate_album) == expected_album:
                candidate_id = str(candidate["id"])
                break
        if not candidate_id:
            return None
        release = self._json(f"/releases/{candidate_id}", params={})
        if _normalized(release.get("title")) != expected_album:
            return None
        release_artist = _normalized(_artist_name(release.get("artists")))
        if release_artist and release_artist != expected_artist:
            return None
        return release_to_details(release)


DISCOGS = DiscogsClient()
