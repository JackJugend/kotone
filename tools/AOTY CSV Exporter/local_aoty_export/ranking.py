"""Conversion of saved AOTY ranking pages to album metadata CSV rows."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .album import ALBUM_HEADERS
from .common import (
    absolute_url,
    aoty_album_id,
    clean_text,
    date_yyyy_mm_dd,
    soup_from_path,
    tag_image_url,
)


RANKING_HEADERS = (
    *ALBUM_HEADERS,
    "Ranking Position",
    "Ranking Scope",
    "Ranking Metric",
    "Ranking Sort",
    "Ranking Genre",
    "Ranking Format",
    "Ranking URL",
    "Ranking Key",
    "Rounded User Score",
    "Amazon URL",
    "Apple Music URL",
    "Spotify URL",
    "Vinyl URL",
    "Bandcamp URL",
    "SoundCloud URL",
)


@dataclass(frozen=True)
class RankingPage:
    rows: list[dict]
    page_url: str
    scope: str


def _node_text(node) -> str:
    return clean_text(node.get_text(" ", strip=True)) if node else ""


def _saved_page_url(path: Path, soup) -> str:
    """Recover the original address recorded by a browser's Ctrl+S save."""

    raw = path.read_bytes().decode("utf-8", errors="replace")
    match = re.search(
        r"saved\s+from\s+url=\(\d+\)(https?://[^\s>]+)",
        raw,
        re.I,
    )
    if match:
        return clean_text(match.group(1))
    canonical = soup.select_one('link[rel="canonical"][href*="/ratings/"]')
    if canonical:
        return absolute_url(canonical.get("href"))
    link = soup.select_one('a[href*="/ratings/user-highest-rated/"]')
    return absolute_url(link.get("href")) if link else ""


def _ranking_context(soup, page_url: str) -> dict[str, str]:
    selector = soup.select_one(".genreSelect")
    data = selector.attrs if selector else {}
    path = urlparse(page_url).path.casefold()
    title = _node_text(soup.select_one("h1.headline"))

    year = clean_text(data.get("data-year"))
    decade = clean_text(data.get("data-decade"))
    release_format = clean_text(data.get("data-release-type"))
    genre_id = clean_text(data.get("data-genre-id"))
    sort = clean_text(data.get("data-sort")) or "weighted"
    genre_label = ""
    genre_selected = soup.select_one("#genre .menuDropSelectedText")
    if genre_selected:
        genre_label = _node_text(genre_selected)
        if genre_label.casefold() == "genre":
            genre_label = ""

    if not re.fullmatch(r"(?:19|20)\d{2}", year):
        match = re.search(r"/(?:19|20)\d{2}(?:/|$)", path)
        year = match.group(0).strip("/") if match else ""
    if not decade:
        match = re.search(r"/((?:19|20)\d0s)(?:/|$)", path)
        decade = match.group(1) if match else ""

    scope = f"year:{year}" if year else f"decade:{decade}" if decade else "all"
    return {
        "scope": scope,
        "year": year,
        "decade": decade,
        "format": release_format,
        "genre_id": genre_id,
        "genre": genre_label,
        "sort": sort,
        "metric": "user_score" if "user score" in title.casefold() or "user-highest-rated" in path else "",
    }


def _split_artist_album(combined: str) -> tuple[str, str]:
    """AOTY ranking rows expose one canonical ``Artist - Album`` label."""

    parts = re.split(r"\s+-\s+", clean_text(combined), maxsplit=1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return "", clean_text(combined)


def _genres(row) -> tuple[list[str], list[str]]:
    box = row.select_one(".albumListGenre")
    if not box:
        return [], []
    secondary_box = box.select_one(".secondary-genres")
    secondary = [
        _node_text(anchor)
        for anchor in secondary_box.select("a") if _node_text(anchor)
    ] if secondary_box else []
    primary = [
        _node_text(anchor)
        for anchor in box.find_all("a", recursive=False) if _node_text(anchor)
    ]
    return primary, secondary


def _must_hear(row) -> str:
    cover = row.select_one(".albumListCover")
    classes = {str(value).casefold() for value in (cover.get("class", []) if cover else [])}
    if "both" in classes:
        return "both"
    if "critic" in classes or "critics" in classes:
        return "critics"
    if "user" in classes or "users" in classes:
        return "users"
    return ""


def _score_and_count(row) -> tuple[str, str, str]:
    container = row.select_one(".scoreValueContainer")
    exact = clean_text(container.get("title")) if container else ""
    rounded = _node_text(row.select_one(".scoreValue"))
    score = exact if re.fullmatch(r"100(?:\.0+)?|\d{1,2}(?:\.\d+)?", exact) else rounded
    count_text = _node_text(row.select_one(".scoreText"))
    count_match = re.search(r"[\d,]+", count_text)
    count = re.sub(r"\D", "", count_match.group(0)) if count_match else ""
    return score, rounded, count


def _external_links(row) -> dict[str, str]:
    result = {
        "Amazon URL": "",
        "Apple Music URL": "",
        "Spotify URL": "",
        "Vinyl URL": "",
        "Bandcamp URL": "",
        "SoundCloud URL": "",
    }
    labels = {
        "amazon": "Amazon URL",
        "apple music": "Apple Music URL",
        "spotify": "Spotify URL",
        "vinyl": "Vinyl URL",
        "bandcamp": "Bandcamp URL",
        "soundcloud": "SoundCloud URL",
    }
    for anchor in row.select(".albumListLinks a[href]"):
        action = clean_text(anchor.get("data-track-action")).casefold()
        label = labels.get(action)
        if label:
            result[label] = absolute_url(anchor.get("href"))
    return result


def parse_ranking_page(path: Path) -> RankingPage:
    soup = soup_from_path(path)
    page_url = _saved_page_url(path, soup)
    context = _ranking_context(soup, page_url)
    ranking_key = "|".join((
        context["metric"],
        context["scope"],
        context["sort"].casefold(),
        (context["genre"] or context["genre_id"]).casefold(),
        context["format"].casefold(),
    ))
    cards = soup.select(".albumListRow[id^='rank-']")
    if not cards:
        raise ValueError("nie znaleziono rekordow rankingu AOTY")

    rows: list[dict] = []
    for card in cards:
        album_link = card.select_one('.albumListTitle a[itemprop="url"][href*="/album/"]')
        album_url = absolute_url(album_link.get("href")) if album_link else ""
        album_id = aoty_album_id(album_url)
        position_node = card.select_one('[itemprop="position"]')
        position = _node_text(position_node)
        if not album_id or not position.isdecimal():
            continue

        combined = _node_text(album_link)
        artist, album = _split_artist_album(combined)
        primary, secondary = _genres(card)
        score, rounded_score, ratings_count = _score_and_count(card)
        image = card.select_one(".albumListCover img")
        release_date = date_yyyy_mm_dd(_node_text(card.select_one(".albumListDate")))
        external_links = _external_links(card)

        # Only unfiltered, weighted all-time/year charts are authoritative for
        # Kotone's two ranking fields. Genre, decade and format ranks remain
        # available in the CSV without overwriting global album rankings.
        authoritative = (
            context["metric"] == "user_score"
            and context["sort"].casefold() == "weighted"
            and not context["genre_id"]
            and not context["genre"]
            and not context["format"]
            and not context["decade"]
        )
        year_rank = f"#{position}" if authoritative and context["year"] else ""
        all_time_rank = f"#{position}" if authoritative and context["scope"] == "all" else ""

        result = {
            "Album ID": album_id,
            "Artist": artist,
            "Artist URL": "",
            "Album": album,
            "Album URL": album_url,
            "Cover URL": tag_image_url(image),
            "Release Date": release_date,
            "Format": context["format"],
            "Duration": "",
            "Label": "",
            "Genres": ", ".join(primary),
            "Secondary Genres": ", ".join(secondary),
            "Vibes": "",
            "AOTY User Score": score,
            "AOTY Ratings": ratings_count,
            "Critic Score": "",
            "Critic Reviews": "",
            "Year Ratings": year_rank,
            "All Time Ratings": all_time_rank,
            "Must Hear": _must_hear(card),
            "Ranking Position": position,
            "Ranking Scope": context["scope"],
            "Ranking Metric": context["metric"],
            "Ranking Sort": context["sort"],
            "Ranking Genre": context["genre"] or context["genre_id"],
            "Ranking Format": context["format"],
            "Ranking URL": page_url,
            "Ranking Key": ranking_key,
            "Rounded User Score": rounded_score,
        }
        result.update(external_links)
        rows.append(result)
    if not rows:
        raise ValueError("rekordy rankingu nie zawieraja poprawnych Album ID")
    return RankingPage(rows=rows, page_url=page_url, scope=context["scope"])
