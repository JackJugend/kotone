"""Small persistent cover cache used only by optional statistics graphics."""

from __future__ import annotations

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import RLock
from urllib.parse import urlparse

import requests

from settings import BASE_DIR, DATA_DIR


MAX_COVER_BYTES = 8 * 1024 * 1024
CACHE_DIR = Path(DATA_DIR or BASE_DIR) / "stats-cover-cache"
_LOCK = RLock()


def _safe_cover_url(value) -> str | None:
    url = str(value or "").strip()
    if url.startswith("//"):
        url = "https:" + url
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    allowed = (
        host == "albumoftheyear.org"
        or host.endswith(".albumoftheyear.org")
        or host == "coverartarchive.org"
        or host.endswith(".coverartarchive.org")
        or host == "archive.org"
        or host.endswith(".archive.org")
    )
    if parsed.scheme != "https" or not allowed:
        return None
    return url


def _cache_path(url: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{digest}.img"


def _load_one(url: str) -> bytes | None:
    safe_url = _safe_cover_url(url)
    if safe_url is None:
        return None
    path = _cache_path(safe_url)
    with _LOCK:
        try:
            cached = path.read_bytes()
            if 0 < len(cached) <= MAX_COVER_BYTES:
                return cached
        except FileNotFoundError:
            pass
        except OSError:
            return None

    try:
        response = requests.get(
            safe_url,
            headers={"User-Agent": "Kotone/1.0 statistics cover cache"},
            timeout=(5, 12),
        )
        response.raise_for_status()
        content = bytes(response.content)
        if not content or len(content) > MAX_COVER_BYTES:
            return None
        if not str(response.headers.get("Content-Type") or "").casefold().startswith("image/"):
            return None
    except requests.RequestException:
        return None

    with _LOCK:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_bytes(content)
            os.replace(temporary, path)
        except OSError:
            pass
    return content


def load_cover_images(items: list[dict], *, limit: int = 3) -> list[dict]:
    """Return up to ``limit`` config-scope items with locally cached bytes."""

    candidates = list(items)[:max(0, int(limit))]
    if not candidates:
        return []
    with ThreadPoolExecutor(max_workers=len(candidates)) as executor:
        contents = list(
            executor.map(
                lambda item: _load_one(item.get("cover")),
                candidates,
            )
        )
    return [
        {**item, "image_bytes": content}
        for item, content in zip(candidates, contents)
        if content
    ]
