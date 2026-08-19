"""AOTY orange Must Hear badge rules, evaluated only from cached metadata."""

from __future__ import annotations

import re
import hashlib
import os
from urllib.parse import quote


# AOTY's orange badge is editorial, not a formula based on its two scores.
# These verified pages were manually seeded before the scraper had a durable
# flag. New pages are persisted from the live AOTY marker instead.
KNOWN_AOTY_MUST_HEAR_IDS = frozenset({
    "10879",  # Four-Calendar CafÃ©
    "4594",   # Heaven or Las Vegas
    "104775", # The Moon and the Melodies
    "6490",   # Treasure
    "7346",   # Head Over Heels
})

# Discord caches generated thumbnail URLs very aggressively. Bump this when
# the deterministic badge artwork changes so a freshly invoked command shows
# the new rendering instead of a previously cached PNG.
MUST_HEAR_BADGE_RENDER_VERSION = "2"


def numeric_count(value) -> float | None:
    text = str(value or "").strip().upper().replace(",", "")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([KM]?)", text)
    if not match:
        return None
    number = float(match.group(1))
    multiplier = {"": 1, "K": 1_000, "M": 1_000_000}[match.group(2)]
    return number * multiplier


def must_hear_album(
    user_score,
    ratings_count,
    critic_score,
    critic_reviews_count,
    *,
    album_id: str | None = None,
    official: bool | None = None,
) -> bool:
    """Return AOTY's orange tag, preferring its explicit editorial marker."""

    if official is not None:
        return bool(official)
    if str(album_id or "").strip() in KNOWN_AOTY_MUST_HEAR_IDS:
        return True

    # Compatibility fallback for legacy cache rows that predate the explicit
    # AOTY marker. It is never used once the release is refreshed from AOTY.
    try:
        user = float(user_score)
        critic = float(critic_score)
    except (TypeError, ValueError):
        return False
    users = numeric_count(ratings_count)
    critics = numeric_count(critic_reviews_count)
    return bool(
        user > 80
        and critic < 80
        and users is not None
        and users >= 500
        and critics is not None
        and critics >= 15
    )


def must_hear_kind(user_score, critic_score) -> str:
    """Classify AOTY's three Must Hear score relationships for presentation."""

    try:
        user = float(user_score)
    except (TypeError, ValueError):
        user = None
    try:
        critic = float(critic_score)
    except (TypeError, ValueError):
        critic = None

    if user is not None and user > 80 and critic is not None and critic > 80:
        return "both"
    if critic is not None and critic > 80 and (user is None or user <= 80):
        return "critics"
    # An editorial/legacy Must Hear page without both cached scores defaults
    # to the orange community marker rather than hiding the status entirely.
    return "users"


def cover_token(album_id: str, cover_url: str) -> str:
    """Return an endpoint token which stays valid when a provider changes art.

    The generated image is looked up only from the bot's own in-scope release
    cache.  Binding the public URL to the artwork URL made perfectly valid
    Discord thumbnails fail whenever Last.fm/AOTY refreshed that URL between
    rendering a command and Discord downloading its image.
    """

    del cover_url  # Kept in the signature for existing callers/tests.
    payload = (
        f"{MUST_HEAR_BADGE_RENDER_VERSION}|{str(album_id or '').strip()}"
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:20]


def marked_cover_url(album_id: str, cover_url: str) -> str | None:
    """Return Kotone's public marked-cover endpoint when Railway exposes one."""

    album_id = str(album_id or "").strip()
    cover_url = str(cover_url or "").strip()
    domain = str(os.getenv("RAILWAY_PUBLIC_DOMAIN") or "").strip()
    if not album_id or not cover_url or not domain:
        return None
    token = cover_token(album_id, cover_url)
    return (
        f"https://{domain}/must-hear-cover/"
        f"{quote(album_id, safe='')}/{token}.png"
        f"?cover={quote(cover_url, safe='')}"
    )


def marked_cover_endpoint_enabled() -> bool:
    """Whether this deployment can expose generated cover images publicly."""

    return bool(str(os.getenv("RAILWAY_PUBLIC_DOMAIN") or "").strip())
