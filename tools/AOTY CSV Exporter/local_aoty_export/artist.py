"""Conversion of saved AOTY artist pages to a compact CSV cache."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .common import absolute_url, aoty_album_id, clean_text, image_url, page_url_from_file, soup_from_path, split_values, tag_image_url


ARTIST_HEADERS = (
    "Artist", "Artist URL", "Image URL", "Genres", "Aliases", "Members",
    "Origin Country", "Founded/Birthdate", "Album ID", "Album", "Album URL",
    "Year", "Format", "Cover URL",
)


@dataclass(frozen=True)
class ArtistPage:
    rows: list[dict]


def _detail_values(soup, label: str) -> list[str]:
    """Read one row from the artist-details box only.

    AOTY pages include navigation, filter controls and the logged-in user's
    menu in the document.  Looking through the whole page makes labels such
    as ``Genre`` and ``Aliases`` swallow that UI text, so this intentionally
    accepts values only from the compact artist header details box.
    """

    wanted = label.casefold()
    for row in soup.select(".artistHeader .artistTopBox.info .detailRow"):
        marker_tag = row.select_one("span")
        marker = clean_text(
            marker_tag.get_text(" ", strip=True) if marker_tag is not None else ""
        )
        if wanted not in marker.casefold():
            continue
        values = [
            clean_text(link.get_text(" ", strip=True))
            for link in row.select("a")
            if clean_text(link.get_text(" ", strip=True))
        ]
        if values:
            return values
        direct_text = " ".join(
            clean_text(part)
            for part in row.find_all(string=True, recursive=False)
            if clean_text(part)
        )
        return split_values(direct_text)
    return []


def _release_cards(soup):
    """Return only actual discography cards, never navigation album links."""

    return soup.select("#albumOutput .albumBlock")


def parse_artist_page(path: Path) -> ArtistPage:
    soup = soup_from_path(path)
    artist_url = page_url_from_file(soup, path)
    heading = soup.select_one(".artistHeader h1.artistHeadline") or soup.select_one("h1")
    artist = clean_text(heading.get_text(" ", strip=True)) if heading else ""
    if not artist:
        meta = soup.select_one('meta[property="og:title"]')
        artist = clean_text(meta.get("content")) if meta else ""
    if not artist:
        raise ValueError("nie znaleziono nazwy artysty")
    common = {
        "Artist": artist,
        "Artist URL": artist_url,
        "Image URL": image_url(soup),
        "Genres": ", ".join(_detail_values(soup, "genre")),
        "Aliases": ", ".join(_detail_values(soup, "also known as")),
        "Members": ", ".join(_detail_values(soup, "member")),
        "Origin Country": ", ".join(_detail_values(soup, "origin")),
        "Founded/Birthdate": ", ".join(
            _detail_values(soup, "founded") or _detail_values(soup, "born")
        ),
    }
    rows: list[dict] = []
    seen: set[str] = set()
    for card in _release_cards(soup):
        link = card.select_one('a[href*="/album/"]')
        if link is None:
            continue
        album_url = absolute_url(link.get("href"))
        album_id = aoty_album_id(album_url)
        title = card.select_one(".albumTitle")
        album = clean_text(title.get_text(" ", strip=True)) if title else clean_text(link.get_text(" ", strip=True))
        if not album_id or not album or album_id in seen:
            continue
        year = clean_text((card.select_one(".type") or "").get_text(" ", strip=True))
        year = year if year.isdecimal() and len(year) == 4 else ""
        cover = card.select_one(".image img[src]")
        rows.append({
            **common,
            "Album ID": album_id,
            "Album": album,
            "Album URL": album_url,
            "Year": year,
            "Format": "",
            "Cover URL": tag_image_url(cover),
        })
        seen.add(album_id)
    if not rows:
        rows.append({**common, "Album ID": "", "Album": "", "Album URL": "", "Year": "", "Format": "", "Cover URL": ""})
    return ArtistPage(rows=rows)
