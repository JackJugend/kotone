"""AOTY orange Must Hear badge rules, evaluated only from cached metadata."""

from __future__ import annotations

import re
import hashlib
import os
from urllib.parse import quote


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
) -> bool:
    """Return the public AOTY orange-tag condition from cached values."""

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


def cover_token(album_id: str, cover_url: str) -> str:
    payload = f"{album_id}|{cover_url}".encode("utf-8")
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
    )
