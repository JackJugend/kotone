"""Conversion of saved AOTY album pages to metadata and tracklist CSV files."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .common import absolute_url, aoty_album_id, clean_text, image_url, page_url_from_file, soup_from_path, split_values


ALBUM_HEADERS = (
    "Album ID", "Artist", "Artist URL", "Album", "Album URL", "Cover URL",
    "Release Date", "Format", "Duration", "Label", "Genres", "Secondary Genres",
    "Vibes", "AOTY User Score", "AOTY Ratings", "Critic Score", "Critic Reviews",
    "Year Ratings", "All Time Ratings", "Must Hear",
)
TRACK_HEADERS = ("Album ID", "Number", "Disc", "Title", "Duration", "AOTY Score", "URL")


@dataclass(frozen=True)
class AlbumPage:
    metadata: dict
    tracks: list[dict]


def _value_after_label(text: str, label: str) -> str:
    match = re.search(rf"{label}\s*[:]?\s*([^|•\n]+)", text, re.I)
    return clean_text(match.group(1)) if match else ""


def _find_text_for_label(soup, labels: tuple[str, ...]) -> str:
    for node in soup.find_all(string=re.compile("|".join(re.escape(label) for label in labels), re.I)):
        parent = node.parent
        text = clean_text(parent.parent.get_text(" ", strip=True) if parent and parent.parent else parent.get_text(" ", strip=True))
        for label in labels:
            value = _value_after_label(text, label)
            if value:
                return value
    return ""


def _score(text: str, label: str) -> str:
    match = re.search(rf"{label}\s*(?:score)?\s*[:]?\s*(\d{{1,3}}(?:\.\d+)?)", text, re.I)
    return match.group(1) if match else ""


def parse_album_page(path: Path) -> AlbumPage:
    soup = soup_from_path(path)
    album_url = page_url_from_file(soup, path)
    album_id = aoty_album_id(album_url)
    if not album_id:
        for tag in soup.select('a[href*="/album/"]'):
            album_id = aoty_album_id(tag.get("href"))
            if album_id:
                album_url = absolute_url(tag.get("href"))
                break
    if not album_id:
        raise ValueError("nie znaleziono AOTY album_id")

    title = clean_text((soup.select_one("h1") or soup.select_one('meta[property="og:title"]')).get("content") if soup.select_one('meta[property="og:title"]') and not soup.select_one("h1") else (soup.select_one("h1").get_text(" ", strip=True) if soup.select_one("h1") else ""))
    artist_link = soup.find("a", href=re.compile(r"/artist/", re.I))
    artist = clean_text(artist_link.get_text(" ", strip=True)) if artist_link else ""
    artist_url = absolute_url(artist_link.get("href")) if artist_link else ""
    if " - " in title and not artist:
        artist, title = [clean_text(part) for part in title.split(" - ", 1)]
    if not title:
        title_tag = soup.select_one('meta[property="og:title"]')
        title = clean_text(title_tag.get("content")) if title_tag else f"Album #{album_id}"

    text = clean_text(soup.get_text(" ", strip=True))
    release_date = _find_text_for_label(soup, ("release date",))
    release_format = _find_text_for_label(soup, ("format",))
    label = _find_text_for_label(soup, ("label",))
    duration = _find_text_for_label(soup, ("duration", "length"))
    genres = _find_text_for_label(soup, ("genre",))
    secondary = _find_text_for_label(soup, ("secondary genre",))
    vibes = _find_text_for_label(soup, ("vibe",))
    metadata = {
        "Album ID": album_id,
        "Artist": artist,
        "Artist URL": artist_url,
        "Album": title,
        "Album URL": album_url,
        "Cover URL": image_url(soup),
        "Release Date": release_date,
        "Format": release_format,
        "Duration": duration,
        "Label": label,
        "Genres": ", ".join(split_values(genres)),
        "Secondary Genres": ", ".join(split_values(secondary)),
        "Vibes": ", ".join(split_values(vibes)),
        "AOTY User Score": _score(text, "user"),
        "AOTY Ratings": _value_after_label(text, "ratings"),
        "Critic Score": _score(text, "critic"),
        "Critic Reviews": _value_after_label(text, "reviews"),
        "Year Ratings": _value_after_label(text, r"(?:20|19)\d{2} ratings"),
        "All Time Ratings": _value_after_label(text, "all time"),
        "Must Hear": "both" if "must hear" in text.casefold() else "",
    }

    tracks: list[dict] = []
    seen: set[str] = set()
    for link in soup.select('a[href*="/song/"]'):
        url = absolute_url(link.get("href"))
        title_text = clean_text(link.get_text(" ", strip=True))
        if not title_text or url in seen:
            continue
        parent = link
        for _ in range(4):
            parent = parent.parent
            if parent is None:
                break
            parent_text = clean_text(parent.get_text(" ", strip=True))
            if len(parent_text) >= len(title_text):
                break
        parent_text = clean_text(parent.get_text(" ", strip=True)) if parent else title_text
        number_match = re.search(r"(?:^|\s)(\d{1,2})[.)]", parent_text)
        duration_match = re.search(r"\b(\d{1,2}:\d{2})\b", parent_text)
        score_match = re.search(r"\b(100|[1-9]?\d)\b", parent_text[len(title_text):])
        tracks.append({
            "Album ID": album_id,
            "Number": number_match.group(1) if number_match else str(len(tracks) + 1),
            "Disc": "1",
            "Title": title_text,
            "Duration": duration_match.group(1) if duration_match else "",
            "AOTY Score": score_match.group(1) if score_match else "",
            "URL": url,
        })
        seen.add(url)
    return AlbumPage(metadata=metadata, tracks=tracks)
