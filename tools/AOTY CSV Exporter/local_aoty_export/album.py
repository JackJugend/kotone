"""Conversion of saved AOTY album pages to metadata and tracklist CSV files."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .common import absolute_url, aoty_album_id, clean_text, image_url, page_url_from_file, soup_from_path


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


def _node_text(node) -> str:
    return clean_text(node.get_text(" ", strip=True)) if node else ""


def _score_from_box(box, score_selector: str) -> str:
    node = box.select_one(score_selector) if box else None
    if not node:
        return ""
    raw = node.get("data-btx-orig-score") or node.get("data-btx-original-score") or _node_text(node)
    match = re.search(r"\b(100(?:\.0+)?|\d{1,2}(?:\.\d+)?)\b", str(raw))
    return match.group(1) if match else ""


def _count_from_box(box, noun: str) -> str:
    if not box:
        return ""
    for node in box.select(".numReviews"):
        text = _node_text(node)
        if re.search(rf"\b{re.escape(noun)}\b", text, re.I):
            strong = node.select_one("strong")
            raw = _node_text(strong) if strong else text
            match = re.search(r"\b[\d,]+\b", raw)
            return re.sub(r"\D", "", match.group(0)) if match else ""
    return ""


def _details(soup) -> dict[str, object]:
    """Read only the structured Details box, never navigation/page text."""

    result: dict[str, object] = {
        "release_date": "", "format": "", "label": "",
        "genres": [], "secondary": [], "vibes": [],
    }
    box = soup.select_one(".albumTopBox.info")
    if not box:
        return result

    for row in box.select(":scope > .detailRow"):
        marker = row.select_one(":scope > span")
        marker_text = _node_text(marker).casefold()
        if not marker_text:
            continue
        prefix = clean_text(" ".join(row.stripped_strings).split("/", 1)[0])
        if "release date" in marker_text:
            result["release_date"] = prefix
        elif "format" in marker_text:
            result["format"] = prefix
        elif "label" in marker_text:
            result["label"] = ", ".join(_node_text(a) for a in row.select(":scope > a") if _node_text(a)) or prefix
        elif "genre" in marker_text:
            primary: list[str] = []
            secondary: list[str] = []
            for anchor in row.select(":scope > a"):
                value = _node_text(anchor)
                if not value:
                    continue
                (secondary if anchor.select_one(".secondary") else primary).append(value)
            result["genres"] = primary
            result["secondary"] = secondary
        elif "vibe" in marker_text:
            result["vibes"] = [_node_text(a) for a in row.select(".vibe > a") if _node_text(a)]
    return result


def _must_hear_kind(soup) -> str:
    node = soup.select_one(".albumHeadline .mustHearButton")
    if not node:
        return ""
    classes = {str(item).casefold() for item in node.get("class", [])}
    title = str(node.get("title") or "").casefold()
    if "both" in classes or "both" in title:
        return "both"
    if "critic" in classes or "critic" in title:
        return "critics"
    if "user" in classes or "user" in title:
        return "users"
    return ""


def _rankings(user_box) -> tuple[str, str]:
    year_ranking = ""
    all_time_ranking = ""
    if not user_box:
        return year_ranking, all_time_ranking
    for node in user_box.select(".text.gray"):
        text = _node_text(node)
        year_match = re.search(r"((?:19|20)\d{2})\s+Ratings:\s*#(\d+)", text, re.I)
        if year_match:
            year_ranking = f"{year_match.group(1)}:#{year_match.group(2)}"
        all_time_match = re.search(r"All\s+Time:\s*#(\d+)", text, re.I)
        if all_time_match:
            all_time_ranking = f"#{all_time_match.group(1)}"
    return year_ranking, all_time_ranking


def parse_album_page(path: Path) -> AlbumPage:
    soup = soup_from_path(path)
    album_url = page_url_from_file(soup, path)
    album_id = aoty_album_id(album_url)
    if not album_id:
        canonical = soup.select_one('link[rel="canonical"][href*="/album/"]')
        album_url = absolute_url(canonical.get("href")) if canonical else album_url
        album_id = aoty_album_id(album_url)
    if not album_id:
        raise ValueError("nie znaleziono AOTY album_id")

    title = _node_text(soup.select_one(".albumHeadline h1.albumTitle"))
    artist_link = soup.select_one('.albumHeadline .artist a[href*="/artist/"]')
    artist = _node_text(artist_link)
    artist_url = absolute_url(artist_link.get("href")) if artist_link else ""
    if not title:
        title_tag = soup.select_one('meta[property="og:title"]')
        title = clean_text(title_tag.get("content")) if title_tag else f"Album #{album_id}"

    details = _details(soup)
    critic_box = soup.select_one(".albumCriticScoreBox")
    user_box = soup.select_one(".albumUserScoreBox")
    year_ranking, all_time_ranking = _rankings(user_box)
    total_length = soup.select_one("#tracklist .totalLength, .rightBox.trackList .totalLength")
    duration = re.sub(r"^Total\s+Length:\s*", "", _node_text(total_length), flags=re.I)

    metadata = {
        "Album ID": album_id,
        "Artist": artist,
        "Artist URL": artist_url,
        "Album": title,
        "Album URL": album_url,
        "Cover URL": image_url(soup),
        "Release Date": details["release_date"],
        "Format": details["format"],
        "Duration": duration,
        "Label": details["label"],
        "Genres": ", ".join(details["genres"]),
        "Secondary Genres": ", ".join(details["secondary"]),
        "Vibes": ", ".join(details["vibes"]),
        "AOTY User Score": _score_from_box(user_box, ".albumUserScore"),
        "AOTY Ratings": _count_from_box(user_box, "ratings"),
        "Critic Score": _score_from_box(critic_box, ".albumCriticScore"),
        "Critic Reviews": _count_from_box(critic_box, "reviews"),
        "Year Ratings": year_ranking,
        "All Time Ratings": all_time_ranking,
        "Must Hear": _must_hear_kind(soup),
    }

    tracks: list[dict] = []
    for row in soup.select("#tracklist table.trackListTable tr"):
        title_link = row.select_one('td.trackTitle a[href*="/song/"]')
        if not title_link:
            continue
        number_match = re.search(r"\d+", _node_text(row.select_one("td.trackNumber")))
        score_node = row.select_one("td.trackRating")
        score_raw = ""
        if score_node:
            score_raw = score_node.get("data-btx-orig-score") or ""
            inner = score_node.select_one("[data-btx-orig-score]")
            if inner:
                score_raw = inner.get("data-btx-orig-score") or score_raw
            if not score_raw:
                score_raw = _node_text(score_node)
        score_match = re.fullmatch(r"\s*(100|\d{1,2})(?:\.0+)?\s*", str(score_raw))
        tracks.append({
            "Album ID": album_id,
            "Number": number_match.group(0) if number_match else str(len(tracks) + 1),
            "Disc": "1",
            "Title": _node_text(title_link),
            "Duration": _node_text(row.select_one("td.trackTitle .length")),
            "AOTY Score": score_match.group(1) if score_match else "",
            "URL": absolute_url(title_link.get("href")),
        })
    return AlbumPage(metadata=metadata, tracks=tracks)
