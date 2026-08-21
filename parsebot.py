"""Oszczędny klient Parse dla publicznych metadanych Album of the Year.

To nie jest obejście zabezpieczeń AOTY. Kotone pyta niezależne, płatne API
Parse wyłącznie w tle, gdy bezpośredni scraper AOTY jest w cooldownie.
"""

from __future__ import annotations

from typing import Any

import requests

from settings import PARSE_API_KEY, PARSE_REQUEST_TIMEOUT


BASE_URL = "https://api.parse.bot/scraper/d9c42eb6-5bec-496f-a97d-2e8b9cc8521d"


class ParseUnavailable(RuntimeError):
    """Parse nie może obecnie bezpiecznie zwrócić danych."""


def _values(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [part.strip() for part in value.split(",") if part.strip()]
    return []


def _payload_data(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data", payload)
    return data if isinstance(data, dict) else {}


class ParseClient:
    """Mały klient jednego endpointu szczegółów albumu."""

    def __init__(self) -> None:
        self.session = requests.Session()

    def status(self) -> dict[str, bool]:
        return {"configured": bool(PARSE_API_KEY)}

    def lookup_album(self, aoty_url: object) -> dict[str, Any] | None:
        """Pobierz publiczne szczegóły po znanej ścieżce AOTY."""

        if not PARSE_API_KEY:
            raise ParseUnavailable("Brak PARSE_API_KEY.")
        url = str(aoty_url or "").strip()
        marker = "albumoftheyear.org"
        if marker not in url:
            return None
        path = "/" + url.split(marker, 1)[1].lstrip("/")
        response = self.session.get(
            f"{BASE_URL}/get_album_details",
            params={"path": path},
            headers={"X-API-Key": PARSE_API_KEY, "Accept": "application/json"},
            timeout=PARSE_REQUEST_TIMEOUT,
        )
        if response.status_code in {401, 403, 429} or response.status_code >= 500:
            raise ParseUnavailable(f"Parse HTTP {response.status_code}.")
        response.raise_for_status()
        data = _payload_data(response.json())
        if not data:
            return None
        extra = data.get("extra_details") if isinstance(data.get("extra_details"), dict) else {}
        labels = _values(data.get("labels") or data.get("label"))
        genres = _values(data.get("genres"))
        release_date = str(data.get("release_date") or "").strip() or None
        return {
            "artist": str(data.get("artist") or "").strip() or None,
            "album": str(data.get("album_title") or data.get("album") or "").strip() or None,
            "user_score": data.get("user_score"),
            "ratings_count": data.get("user_reviews") or data.get("ratings_count"),
            "critic_score": data.get("critic_score"),
            "critic_reviews_count": data.get("critic_reviews"),
            "release_date": release_date,
            "year": release_date[:4] if release_date and len(release_date) >= 4 else None,
            "album_format": extra.get("Format") or data.get("format"),
            "labels": labels,
            "label": ", ".join(labels) or None,
            "genres": genres,
            "external_metadata": {"parse_aoty_path": path, "parse_aoty_url": url},
            "_section_complete": {
                "score": any(data.get(key) is not None for key in ("user_score", "critic_score")),
                "release_date": bool(release_date),
                "format": bool(extra.get("Format") or data.get("format")),
                "labels": bool(labels),
                "genres": bool(genres),
            },
        }


PARSE = ParseClient()
