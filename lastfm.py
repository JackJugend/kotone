"""Small, cached Last.fm artist-image lookup.

This intentionally fetches only an artist's public Open Graph image. It never
touches AOTY or MusicBrainz and the caller persists both success and failure
timestamps, so a Discord command cannot repeatedly probe Last.fm.
"""

from __future__ import annotations

from html.parser import HTMLParser
from urllib.parse import quote_plus

import requests


BASE_URL = "https://www.last.fm/music/"
USER_AGENT = "Kotone/1.0 (https://github.com/JackJugend/kotone)"
PLACEHOLDER_FRAGMENT = "2a96cbd8b46e442fc41c2b86b821562f"


class LastFMUnavailable(RuntimeError):
    """Last.fm did not return a usable artist page."""


class _OpenGraphParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.image: str | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.casefold() != "meta" or self.image:
            return
        values = {str(key).casefold(): str(value or "") for key, value in attrs}
        if values.get("property", "").casefold() == "og:image":
            self.image = values.get("content", "").strip() or None


def fetch_artist_image(artist: object, *, session=None) -> str | None:
    """Return a usable Last.fm artist image, or ``None`` when unavailable."""

    name = str(artist or "").strip()
    if not name:
        return None
    client = session or requests.Session()
    try:
        response = client.get(
            BASE_URL + quote_plus(name),
            headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
            timeout=(5, 12),
        )
    except requests.RequestException as exc:
        raise LastFMUnavailable(str(exc)) from exc

    if response.status_code in {403, 406, 429, 502, 503, 504}:
        raise LastFMUnavailable(f"HTTP {response.status_code}")
    if response.status_code == 404:
        return None
    try:
        response.raise_for_status()
    except requests.RequestException as exc:
        raise LastFMUnavailable(str(exc)) from exc

    parser = _OpenGraphParser()
    parser.feed(response.text)
    image = str(parser.image or "").strip()
    if (
        not image.startswith("https://")
        or PLACEHOLDER_FRAGMENT in image.casefold()
    ):
        return None
    return image
