"""Conversion of saved AOTY artist pages to a compact CSV cache."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .common import absolute_url, aoty_album_id, clean_text, image_url, page_url_from_file, soup_from_path, split_values


ARTIST_HEADERS = (
    "Artist", "Artist URL", "Image URL", "Genres", "Aliases", "Members",
    "Origin Country", "Founded/Birthdate", "Album ID", "Album", "Album URL",
    "Year", "Format", "Cover URL",
)


@dataclass(frozen=True)
class ArtistPage:
    rows: list[dict]


def parse_artist_page(path: Path) -> ArtistPage:
    soup = soup_from_path(path)
    artist_url = page_url_from_file(soup, path)
    heading = soup.select_one("h1")
    artist = clean_text(heading.get_text(" ", strip=True)) if heading else ""
    if not artist:
        meta = soup.select_one('meta[property="og:title"]')
        artist = clean_text(meta.get("content")) if meta else ""
    if not artist:
        raise ValueError("nie znaleziono nazwy artysty")
    text = clean_text(soup.get_text(" ", strip=True))
    genre_match = re.search(r"(?:genre|genres)\s*[:]?\s*([^|•]+)", text, re.I)
    aliases_match = re.search(r"(?:also known as|aliases)\s*[:]?\s*([^|•]+)", text, re.I)
    member_match = re.search(r"(?:members?)\s*[:]?\s*([^|•]+)", text, re.I)
    country_match = re.search(r"(?:origin|country)\s*[:]?\s*([^|•]+)", text, re.I)
    date_match = re.search(r"(?:founded|born|birthdate)\s*[:]?\s*([^|•]+)", text, re.I)
    common = {
        "Artist": artist,
        "Artist URL": artist_url,
        "Image URL": image_url(soup),
        "Genres": ", ".join(split_values(genre_match.group(1) if genre_match else "")),
        "Aliases": ", ".join(split_values(aliases_match.group(1) if aliases_match else "")),
        "Members": clean_text(member_match.group(1) if member_match else ""),
        "Origin Country": clean_text(country_match.group(1) if country_match else ""),
        "Founded/Birthdate": clean_text(date_match.group(1) if date_match else ""),
    }
    rows: list[dict] = []
    seen: set[str] = set()
    for link in soup.select('a[href*="/album/"]'):
        album_url = absolute_url(link.get("href"))
        album_id = aoty_album_id(album_url)
        album = clean_text(link.get_text(" ", strip=True))
        if not album_id or not album or album_id in seen:
            continue
        parent = link
        for _ in range(4):
            parent = parent.parent
            if parent is None:
                break
        card_text = clean_text(parent.get_text(" ", strip=True)) if parent else album
        year = re.search(r"\b((?:19|20)\d{2})\b", card_text)
        release_format = re.search(r"\b(LP|EP|Single|Mixtape|Compilation|Reissue|Music Video|DJ Mix)\b", card_text, re.I)
        cover = parent.select_one("img[src]") if parent else None
        rows.append({
            **common,
            "Album ID": album_id,
            "Album": album,
            "Album URL": album_url,
            "Year": year.group(1) if year else "",
            "Format": release_format.group(1) if release_format else "",
            "Cover URL": absolute_url(cover.get("src")) if cover else "",
        })
        seen.add(album_id)
    if not rows:
        rows.append({**common, "Album ID": "", "Album": "", "Album URL": "", "Year": "", "Format": "", "Cover URL": ""})
    return ArtistPage(rows=rows)
