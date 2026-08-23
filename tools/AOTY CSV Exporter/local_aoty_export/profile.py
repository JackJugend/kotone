"""Conversion of saved AOTY profile-rating pages to import-ready CSV."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .common import (
    absolute_url,
    aoty_album_id,
    aoty_profile_name,
    clean_text,
    date_yyyy_mm_dd,
    page_url_from_file,
    soup_from_path,
)


PROFILE_HEADERS = (
    "Artist", "Album", "Year", "Type", "Rating", "Date Rated",
    "Album ID", "Album URL", "Artist URL", "Cover URL",
)


@dataclass(frozen=True)
class ProfilePage:
    username: str
    rows: list[dict]


def _ancestor_with_context(tag):
    node = tag
    for _ in range(6):
        if node is None:
            break
        text = clean_text(node.get_text(" ", strip=True))
        if len(text) >= 8:
            return node
        node = node.parent
    return tag.parent


def _first_number(text: str, pattern: str) -> str:
    match = re.search(pattern, text, re.I)
    return match.group(1) if match else ""


def parse_profile_page(path: Path) -> ProfilePage:
    soup = soup_from_path(path)
    page_url = page_url_from_file(soup, path)
    username = aoty_profile_name(page_url)
    if not username:
        for tag in soup.select('a[href*="/user/"]'):
            username = aoty_profile_name(tag.get("href"))
            if username:
                break
    if not username:
        raise ValueError("nie znaleziono nazwy profilu AOTY w zapisanym HTML")

    seen: set[str] = set()
    rows: list[dict] = []
    for album_link in soup.select('a[href*="/album/"]'):
        album_url = absolute_url(album_link.get("href"))
        album_id = aoty_album_id(album_url)
        album = clean_text(album_link.get_text(" ", strip=True))
        if not album_id or not album or album_id in seen:
            continue
        context = _ancestor_with_context(album_link)
        text = clean_text(context.get_text(" ", strip=True))
        artist_link = context.find("a", href=re.compile(r"/artist/", re.I))
        artist = clean_text(artist_link.get_text(" ", strip=True)) if artist_link else ""
        artist_url = absolute_url(artist_link.get("href")) if artist_link else ""
        if not artist:
            before = clean_text(text.split(album, 1)[0])
            artist = re.sub(r"\b(?:rated|rating|score)\b.*$", "", before, flags=re.I).strip(" -–—")
        score = _first_number(text, r"(?:rating|score)\s*[:]?\s*(100|[1-9]?\d)(?!\d)")
        if not score:
            score_tag = context.select_one(".rating, .score, .userScore, .ratingValue")
            score = _first_number(clean_text(score_tag.get_text(" ", strip=True)) if score_tag else "", r"(100|[1-9]?\d)(?!\d)")
        year = _first_number(text, r"\b((?:19|20)\d{2})\b")
        release_type = _first_number(text, r"\b(LP|EP|Single|Mixtape|Compilation|Reissue|Music Video|DJ Mix)\b")
        date = date_yyyy_mm_dd(text)
        image = context.select_one("img[src]")
        if artist and score and date:
            rows.append({
                "Artist": artist,
                "Album": album,
                "Year": year,
                "Type": release_type or "LP",
                "Rating": score,
                "Date Rated": date,
                "Album ID": album_id,
                "Album URL": album_url,
                "Artist URL": artist_url,
                "Cover URL": absolute_url(image.get("src")) if image else "",
            })
            seen.add(album_id)
    if not rows:
        raise ValueError("nie znaleziono pełnych rekordów ocen z datą")
    return ProfilePage(username=username, rows=rows)
