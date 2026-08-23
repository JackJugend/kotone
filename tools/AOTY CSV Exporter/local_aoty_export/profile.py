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


def _first_number(text: str, pattern: str) -> str:
    match = re.search(pattern, text, re.I)
    return match.group(1) if match else ""


def _rating_cards(soup):
    """Select real release cards, never AOTY's header, menu or footer links."""

    selectors = (
        "#albumOutput .albumBlock",
        ".ratings .albumBlock",
        ".profileRatings .albumBlock",
        ".albumList .albumBlock",
    )
    cards = []
    seen: set[int] = set()
    for selector in selectors:
        for card in soup.select(selector):
            marker = id(card)
            if marker not in seen:
                cards.append(card)
                seen.add(marker)
    return cards


def _card_score(card, text: str) -> str:
    for selector in (".rating[data-btx-orig-score]", ".rating", ".userScore", ".score"):
        tag = card.select_one(selector)
        if tag is None:
            continue
        value = _first_number(clean_text(tag.get_text(" ", strip=True)), r"(100|[1-9]?\d)(?!\d)")
        if value:
            return value
    return _first_number(text, r"(?:my\s+score|rating|score)\s*[:]?\s*(100|[1-9]?\d)(?!\d)")


def _card_date(card, text: str) -> str:
    for selector in ("time[datetime]", ".ratingDate", ".date", ".ratedDate"):
        tag = card.select_one(selector)
        if tag is None:
            continue
        value = date_yyyy_mm_dd(tag.get("datetime") or tag.get_text(" ", strip=True))
        if value:
            return value
    return date_yyyy_mm_dd(text)


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
    for context in _rating_cards(soup):
        album_link = context.select_one('a[href*="/album/"]')
        if album_link is None:
            continue
        album_url = absolute_url(album_link.get("href"))
        album_id = aoty_album_id(album_url)
        title = context.select_one(".albumTitle")
        album = clean_text(title.get_text(" ", strip=True)) if title else clean_text(album_link.get_text(" ", strip=True))
        if not album_id or not album or album_id in seen:
            continue
        text = clean_text(context.get_text(" ", strip=True))
        artist_tag = context.select_one(".artistTitle")
        artist_link = context.find("a", href=re.compile(r"/artist/", re.I))
        artist = clean_text(artist_tag.get_text(" ", strip=True)) if artist_tag else ""
        if not artist and artist_link is not None:
            artist = clean_text(artist_link.get_text(" ", strip=True))
        artist_url = absolute_url(artist_link.get("href")) if artist_link else ""
        score = _card_score(context, text)
        year = _first_number(text, r"\b((?:19|20)\d{2})\b")
        release_type = _first_number(text, r"\b(LP|EP|Single|Mixtape|Compilation|Reissue|Music Video|DJ Mix)\b")
        date = _card_date(context, text)
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
        raise ValueError(
            "nie znaleziono kart ocen z datą — zapisz stronę "
            f"https://www.albumoftheyear.org/user/{username}/ratings/ "
            "(nie zwykłą stronę profilu)"
        )
    return ProfilePage(username=username, rows=rows)
