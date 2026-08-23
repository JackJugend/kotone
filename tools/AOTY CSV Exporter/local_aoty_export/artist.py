"""Conversion of saved AOTY artist pages to a compact CSV cache."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .common import absolute_url, aoty_album_id, clean_text, image_url, page_url_from_file, soup_from_path, split_values, tag_image_url


ARTIST_HEADERS = (
    "Artist", "Artist URL", "Image URL", "Genres", "Aliases", "Members",
    "Origin Country", "Founded/Birthdate", "AOTY User Score", "AOTY Ratings",
    "AOTY Followers", "Album ID", "Album", "Album URL", "Year", "Format",
    "Cover URL",
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


def _release_year_and_format(card) -> tuple[str, str]:
    """Read the ``YEAR • FORMAT`` line used by every artist release card.

    The ``?type=all`` view includes every AOTY release kind and sometimes a
    third segment containing featured artists.  The former parser accepted
    only a bare four-digit ``.type`` value, so it discarded both fields on
    real pages such as ``2024 • Reissue``.  ``data-type`` is the most stable
    format source; the visible line remains a safe fallback for saved pages.
    """

    type_tag = card.select_one(".type")
    type_text = clean_text(type_tag.get_text(" ", strip=True)) if type_tag else ""
    year_match = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", type_text)
    year = year_match.group(1) if year_match else ""

    release_format = clean_text(card.get("data-type"))
    if not release_format:
        parts = [clean_text(part) for part in type_text.split("•") if clean_text(part)]
        release_format = parts[1] if len(parts) > 1 else ""
    if not release_format:
        # The default/Featured artist page groups cards under headings such as
        # Albums, Mixtapes and EPs. Those cards deliberately have an empty
        # ``data-type`` and show only the year, so the nearest preceding
        # section heading is the authoritative format. Without this fallback
        # every release could inherit a caller/default format such as EP.
        heading = card.find_previous("h2", class_="subHeadline")
        section = clean_text(heading.get_text(" ", strip=True)).casefold() if heading else ""
        # Some headings contain a nested ``View All`` link, which is UI text
        # rather than part of the release category.
        section = re.sub(r"\s+view all$", "", section).strip()
        section_formats = {
            "albums": "LP",
            "lps": "LP",
            "eps": "EP",
            "mixtapes": "Mixtape",
            "singles": "Single",
            "compilations": "Compilation",
            "reissues": "Reissue",
            "remixes": "Remix",
            "demos": "Demo",
            "live albums": "Live",
            "soundtracks": "Soundtrack",
            "box sets": "Box Set",
            "music videos": "Music Video",
        }
        release_format = section_formats.get(section, "")
    format_labels = {
        "lp": "LP",
        "ep": "EP",
        "dj mix": "DJ Mix",
    }
    release_format = format_labels.get(
        release_format.casefold(),
        release_format.title(),
    )
    return year, release_format


def _artist_summary(soup) -> tuple[str, str, str]:
    user_box = soup.select_one(".artistHeader .artistUserScoreBox")
    score_tag = user_box.select_one(".artistUserScore") if user_box else None
    ratings_tag = user_box.select_one(".text") if user_box else None
    followers_tag = soup.select_one(".artistTopBox .followCount")
    score = clean_text(score_tag.get_text(" ", strip=True)) if score_tag else ""
    score_match = re.search(r"(?<!\d)(100|\d{1,2})(?!\d)", score)
    score = score_match.group(1) if score_match else ""

    # Some browser extensions replace the visible score with an image/custom
    # element before the page is saved.  AOTY's own rating bar still carries
    # the exact score as ``width:79%``, so use it only inside the artist's
    # User Score box when the normal text is absent.
    if not score and user_box is not None:
        rating_value = user_box.select_one('[itemprop="ratingValue"]')
        if rating_value is not None:
            value_text = clean_text(
                rating_value.get("content") or rating_value.get_text(" ", strip=True)
            )
            value_match = re.search(r"(?<!\d)(100|\d{1,2})(?!\d)", value_text)
            score = value_match.group(1) if value_match else ""
    if not score and user_box is not None:
        bar = user_box.select_one(".ratingBar [style*='width']")
        style = str(bar.get("style") or "") if bar is not None else ""
        width_match = re.search(r"width\s*:\s*(100|\d{1,2})(?:\.0+)?%", style, re.I)
        score = width_match.group(1) if width_match else ""

    ratings_text = clean_text(ratings_tag.get_text(" ", strip=True)) if ratings_tag else ""
    followers_text = clean_text(followers_tag.get_text(" ", strip=True)) if followers_tag else ""
    ratings = re.sub(r"\D", "", ratings_text)
    followers = re.sub(r"\D", "", followers_text)
    return score if re.fullmatch(r"100|\d{1,2}", score) else "", ratings, followers


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
    user_score, ratings_count, followers_count = _artist_summary(soup)
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
        "AOTY User Score": user_score,
        "AOTY Ratings": ratings_count,
        "AOTY Followers": followers_count,
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
        year, release_format = _release_year_and_format(card)
        cover = card.select_one(".image img[src]")
        rows.append({
            **common,
            "Album ID": album_id,
            "Album": album,
            "Album URL": album_url,
            "Year": year,
            "Format": release_format,
            "Cover URL": tag_image_url(cover),
        })
        seen.add(album_id)
    if not rows:
        rows.append({**common, "Album ID": "", "Album": "", "Album URL": "", "Year": "", "Format": "", "Cover URL": ""})
    return ArtistPage(rows=rows)
