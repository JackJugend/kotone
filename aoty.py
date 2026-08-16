"""Scraping/search layer for Album of the Year.

No Discord code lives here. Commands and the monitor use the same functions,
so parsing behaviour stays consistent everywhere.
"""

from __future__ import annotations

import difflib
import re
import time
import unicodedata
from datetime import datetime, timedelta
from urllib.parse import quote_plus, urljoin

import requests
from bs4 import BeautifulSoup

from settings import (
    ALBUM_LOOKUP_FALLBACK_LIMIT,
    BASE_URL,
    RATING_FETCH_LIMITS,
    RATING_FORMATS,
)


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/151.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

session = requests.Session()
session.headers.update(HEADERS)


class AOTYRateLimit(Exception):
    pass


class AOTYUserNotFound(Exception):
    pass


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def fetch_page(url: str, expected_url: str | None = None) -> str:
    response = session.get(url, timeout=30)

    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After")
        message = "HTTP 429 - za dużo zapytań"

        if retry_after:
            message += f" (Retry-After: {retry_after}s)"

        raise AOTYRateLimit(message)

    response.raise_for_status()

    if expected_url:
        final_url = response.url.rstrip("/").casefold()
        expected = expected_url.rstrip("/").casefold()

        if final_url != expected:
            raise AOTYUserNotFound()

    return response.text


def aoty_user_exists(username: str) -> bool:
    username = str(username or "").strip()

    if not username:
        return False

    url = f"{BASE_URL}/user/{username}/"
    html = fetch_page(url)
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""

    return " - profile - album of the year" in title.casefold()


def get_user_avatar(username: str) -> str | None:
    url = f"{BASE_URL}/user/{username}/"
    soup = BeautifulSoup(fetch_page(url), "html.parser")

    for image in soup.find_all("img"):
        src = image.get("data-src") or image.get("src") or ""

        if "/user/thumbs/" not in src:
            continue
        if src.endswith("/default.jpg"):
            continue
        if src.startswith("//"):
            src = "https:" + src
        elif src.startswith("/"):
            src = urljoin(BASE_URL, src)

        return src

    return None


# ---------------------------------------------------------------------------
# Generic text helpers
# ---------------------------------------------------------------------------

def clean_text(element) -> str | None:
    if not element:
        return None

    text = element.get_text(" ", strip=True)
    return text or None


def normalize_match_text(text: str) -> str:
    if not text:
        return ""

    text = unicodedata.normalize("NFKD", str(text))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.casefold().replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def fuzzy_match_score(query: str, candidate: str) -> float:
    query_normalized = normalize_match_text(query)
    candidate_normalized = normalize_match_text(candidate)

    if not query_normalized or not candidate_normalized:
        return 0.0
    if query_normalized == candidate_normalized:
        return 1.0
    if query_normalized in candidate_normalized:
        length_ratio = len(query_normalized) / max(len(candidate_normalized), 1)
        return 0.90 + min(0.08, length_ratio * 0.08)

    query_tokens = set(query_normalized.split())
    candidate_tokens = set(candidate_normalized.split())
    token_score = 0.0

    if query_tokens and candidate_tokens:
        token_score = len(query_tokens & candidate_tokens) / len(
            query_tokens | candidate_tokens
        )

    sequence_score = difflib.SequenceMatcher(
        None,
        query_normalized,
        candidate_normalized,
    ).ratio()

    return max(sequence_score, token_score * 0.95)


def extract_album_id(href: str) -> str | None:
    match = re.search(r"/album/(\d+)", str(href or ""))
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# User search autocomplete
# ---------------------------------------------------------------------------
_USER_SEARCH_CACHE: dict[str, tuple[float, list[dict]]] = {}
_USER_SEARCH_CACHE_TTL = 45


def _parse_user_search_results(soup: BeautifulSoup, query: str) -> list[dict]:
    candidates: dict[str, dict] = {}

    for link in soup.select('a[href*="/user/"]'):
        href = str(link.get("href", ""))
        match = re.search(r"/user/([^/?#]+)/?", href)

        if not match:
            continue

        username = match.group(1).strip()

        if not username:
            continue

        display = clean_text(link) or username

        # Odrzucamy oczywiste linki nawigacyjne.
        if display.casefold() in {
            "users",
            "user",
            "community",
            "followers",
            "following",
        }:
            continue

        score = max(
            fuzzy_match_score(query, username),
            fuzzy_match_score(query, display),
        )

        current = candidates.get(username.casefold())

        if current is None or score > current["score"]:
            candidates[username.casefold()] = {
                "username": username,
                "name": display,
                "url": f"{BASE_URL}/user/{username}/",
                "score": score,
            }

    ranked = list(candidates.values())
    ranked.sort(key=lambda item: item["score"], reverse=True)
    return ranked


def search_aoty_users(query: str, limit: int = 10) -> list[dict]:
    query = str(query or "").strip()

    if not query:
        return []

    cache_key = query.casefold()
    now = time.monotonic()
    cached = _USER_SEARCH_CACHE.get(cache_key)

    if cached and now - cached[0] < _USER_SEARCH_CACHE_TTL:
        return cached[1][:limit]

    # AOTY has changed search routing before, so we keep a short ordered set
    # of compatible URLs and stop on the first one that yields user links.
    endpoints = [
        f"{BASE_URL}/search/?q={quote_plus(query)}",
        f"{BASE_URL}/search/users/?q={quote_plus(query)}",
        f"{BASE_URL}/search/user/?q={quote_plus(query)}",
    ]

    results: list[dict] = []

    for url in endpoints:
        try:
            soup = BeautifulSoup(fetch_page(url), "html.parser")
        except (requests.RequestException, AOTYUserNotFound):
            continue

        results = _parse_user_search_results(soup, query)

        if results:
            break

    # Exact username fallback. This also makes autocomplete useful if AOTY's
    # search page changes but direct profile URLs still work.
    if not results:
        try:
            if aoty_user_exists(query):
                results = [{
                    "username": query,
                    "name": query,
                    "url": f"{BASE_URL}/user/{query}/",
                    "score": 1.0,
                }]
        except Exception:
            pass

    results = results[:max(1, int(limit))]
    _USER_SEARCH_CACHE[cache_key] = (now, results)
    return results


# ---------------------------------------------------------------------------
# Artist search / discography
# ---------------------------------------------------------------------------

def _artist_direct_value_to_url(value: str) -> str | None:
    prefix = "aoty_artist:"
    value = str(value or "")

    if not value.startswith(prefix):
        return None

    path_part = value[len(prefix):].strip("/ ")

    if not re.fullmatch(r"\d+(?:-[^/?#]+)?", path_part):
        return None

    return f"{BASE_URL}/artist/{path_part}/"


def search_aoty_artists(query: str, limit: int = 10) -> list[dict]:
    query = str(query or "").strip()

    if not query:
        return []

    direct_url = _artist_direct_value_to_url(query)

    if direct_url:
        try:
            soup = BeautifulSoup(fetch_page(direct_url), "html.parser")
            heading = soup.find("h1")
            name = heading.get_text(" ", strip=True) if heading else query
            return [{
                "name": name,
                "url": direct_url,
                "value": "aoty_artist:" + direct_url.split("/artist/", 1)[1].strip("/"),
                "score": 1.0,
            }]
        except Exception:
            return []

    search_url = f"{BASE_URL}/search/artists/?q={quote_plus(query)}"
    soup = BeautifulSoup(fetch_page(search_url), "html.parser")
    candidates: dict[str, str] = {}

    canonical = soup.find(
        "link",
        rel=lambda value: value and "canonical" in str(value).casefold(),
    )

    if canonical:
        canonical_url = canonical.get("href", "")
        if "/artist/" in canonical_url:
            heading = soup.find("h1")
            name = heading.get_text(" ", strip=True) if heading else query
            candidates[canonical_url] = name

    for link in soup.select('a[href*="/artist/"]'):
        href = link.get("href", "")
        match = re.search(r"/artist/(\d+(?:-[^/?#]+)?)/?", href)

        if not match:
            continue

        name = link.get_text(" ", strip=True)

        if not name or name.casefold() in {
            "artists",
            "highest rated",
            "random",
            "similar artists",
            "related artists",
        }:
            continue

        artist_url = f"{BASE_URL}/artist/{match.group(1)}/"
        candidates.setdefault(artist_url, name)

    ranked = []

    for artist_url, name in candidates.items():
        ranked.append({
            "name": name,
            "url": artist_url,
            "value": "aoty_artist:" + artist_url.split("/artist/", 1)[1].strip("/"),
            "score": fuzzy_match_score(query, name),
        })

    ranked.sort(key=lambda item: item["score"], reverse=True)
    return ranked[:max(1, int(limit))]


def resolve_artist(query: str) -> dict | None:
    query = str(query or "").strip()
    direct_url = _artist_direct_value_to_url(query)

    if direct_url:
        soup = BeautifulSoup(fetch_page(direct_url), "html.parser")
        heading = soup.find("h1")

        if not heading:
            return None

        return {
            "name": heading.get_text(" ", strip=True),
            "url": direct_url,
            "value": query,
            "score": 1.0,
        }

    candidates = search_aoty_artists(query, limit=8)

    if not candidates:
        return None

    best = candidates[0]

    if (
        best["score"] < 0.28
        and normalize_match_text(query) not in normalize_match_text(best["name"])
    ):
        return None

    return best


def _release_container_for_link(link):
    album_match = re.search(r"/album/(\d+)", link.get("href", ""))

    if not album_match:
        return link.parent

    album_id = album_match.group(1)
    node = link
    best = link.parent

    for _ in range(9):
        node = node.parent

        if not node:
            break

        found_ids = set()

        for album_link in node.select('a[href*="/album/"]'):
            found = re.search(r"/album/(\d+)", album_link.get("href", ""))
            if found:
                found_ids.add(found.group(1))

        if len(found_ids) > 1:
            break
        if album_id in found_ids:
            best = node

    return best


def _extract_release_format(text: str) -> str | None:
    if not text:
        return None

    known_formats = [
        "Music Video",
        "Miscellaneous",
        "Instrumental",
        "Compilation",
        "Soundtrack",
        "Audiobook",
        "Unofficial",
        "Mixtape",
        "Reissue",
        "Holiday",
        "Box Set",
        "DJ Mix",
        "Single",
        "Remix",
        "Video",
        "Demo",
        "Live",
        "EP",
        "LP",
        "Bootleg",
    ]

    for release_format in known_formats:
        if re.search(rf"\b{re.escape(release_format)}\b", text, flags=re.IGNORECASE):
            return release_format

    return None


def _format_key_from_label(label: str | None) -> str | None:
    if not label:
        return None

    normalized = re.sub(r"[^a-z0-9]+", "", str(label).casefold())

    for key, info in RATING_FORMATS.items():
        candidate = re.sub(r"[^a-z0-9]+", "", info["label"].casefold())
        if normalized == candidate:
            return key

    return None


def _extract_release_cover(container) -> str | None:
    if not container:
        return None

    image = container.select_one("img")

    if not image:
        return None

    cover = image.get("data-src") or image.get("data-lazy-src") or image.get("src")

    if not cover:
        return None
    if cover.startswith("//"):
        return "https:" + cover
    if cover.startswith("/"):
        return urljoin(BASE_URL, cover)

    return cover


def get_artist_releases(artist_url: str) -> dict:
    artist_base_url = str(artist_url).split("?", 1)[0].rstrip("/") + "/"
    page_url = artist_base_url + "?type=all"
    soup = BeautifulSoup(fetch_page(page_url), "html.parser")

    heading = soup.find("h1")
    artist_name = heading.get_text(" ", strip=True) if heading else "Nieznany artysta"

    artist_image = None
    og_image = soup.find("meta", attrs={"property": "og:image"})
    if og_image:
        artist_image = og_image.get("content")

    releases = []
    seen_ids = set()

    for link in soup.select('a[href*="/album/"]'):
        href = link.get("href", "")
        album_match = re.search(r"/album/(\d+)", href)

        if not album_match:
            continue

        album_id = album_match.group(1)

        if album_id in seen_ids:
            continue

        title = link.get_text(" ", strip=True)
        if not title:
            continue

        container = _release_container_for_link(link)
        container_text = (
            " ".join(container.get_text(" ", strip=True).split())
            if container
            else title
        )

        year_match = re.search(r"\b(?:19|20)\d{2}\b", container_text)
        year = year_match.group(0) if year_match else None
        release_format = _extract_release_format(container_text)

        user_score = None
        score_match = re.search(
            r"\b(100|\d{1,2})\s*user\s*score\b",
            container_text,
            flags=re.IGNORECASE,
        )
        if score_match:
            user_score = score_match.group(1)

        ratings_count = None
        count_match = re.search(
            r"user\s*score\s*\(([\d,.]+(?:\s*[KM])?)\)",
            container_text,
            flags=re.IGNORECASE,
        )
        if count_match:
            ratings_count = count_match.group(1).replace(" ", "")

        album_url = urljoin(BASE_URL, href)

        releases.append({
            "album_id": album_id,
            "title": title,
            "album": title,
            "artist": artist_name,
            "url": album_url,
            "year": year,
            "album_format": release_format,
            "release_format": release_format,
            "user_score": user_score,
            "ratings_count": ratings_count,
            "cover": _extract_release_cover(container),
        })
        seen_ids.add(album_id)

    return {
        "artist": artist_name,
        "url": artist_base_url,
        "image": artist_image,
        "releases": releases,
    }


def rank_artist_releases(releases: list[dict], query: str) -> list[tuple[float, dict]]:
    query = str(query or "").strip()
    direct_id = None

    if query.startswith("aoty_album:"):
        match = re.match(r"(\d+)", query[len("aoty_album:"):])
        if match:
            direct_id = match.group(1)

    ranked = []

    for release in releases:
        if direct_id and release.get("album_id") == direct_id:
            score = 2.0
        else:
            score = fuzzy_match_score(query, release.get("title", ""))
        ranked.append((score, release))

    ranked.sort(key=lambda pair: pair[0], reverse=True)
    return ranked


def resolve_album_for_artist(artist_query: str, album_query: str):
    artist_info = resolve_artist(artist_query)

    if not artist_info:
        return None, None

    discography = get_artist_releases(artist_info["url"])
    ranked = rank_artist_releases(discography["releases"], album_query)

    if not ranked:
        return artist_info, None

    score, release = ranked[0]
    direct_choice = str(album_query).startswith("aoty_album:")

    if not direct_choice and score < 0.28:
        return artist_info, None

    release = dict(release)
    release["match_score"] = score
    return artist_info, release


# ---------------------------------------------------------------------------
# Album details
# ---------------------------------------------------------------------------

def _find_details_row(soup: BeautifulSoup, label_name: str):
    wanted = label_name.strip().casefold()
    candidates = []

    for string in soup.find_all(string=True):
        normalized = " ".join(str(string).split()).casefold()

        if normalized == f"/ {wanted}":
            candidates.insert(0, string)
        elif normalized == wanted:
            candidates.append(string)

    known_labels = {
        "release date",
        "format",
        "label",
        "producer",
        "writer",
        "genre",
        "vibe",
    }

    for label in candidates:
        container = label.parent
        best = container

        for _ in range(7):
            if not container:
                break

            labels_here = set()

            for part in container.stripped_strings:
                normalized = " ".join(str(part).split()).casefold().lstrip("/ ").strip()
                if normalized in known_labels:
                    labels_here.add(normalized)

            if len(labels_here) > 1:
                break
            if wanted in labels_here:
                best = container

            container = container.parent

        if best:
            return best

    return None


def _details_value_text(row, label_name: str) -> str | None:
    if not row:
        return None

    text = " ".join(row.get_text(" ", strip=True).split())
    text = re.sub(
        rf"\s*/\s*{re.escape(label_name)}\s*(?:\+)?\s*$",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = text.strip(" /")
    return text or None


def _is_secondary_genre_link(link, row) -> bool:
    node = link

    while node is not None:
        if getattr(node, "name", None) == "small":
            return True

        classes = " ".join(node.get("class", [])).casefold() if hasattr(node, "get") else ""
        node_id = str(node.get("id", "")).casefold() if hasattr(node, "get") else ""
        style = str(node.get("style", "")).replace(" ", "").casefold() if hasattr(node, "get") else ""
        marker = f"{classes} {node_id}"

        if any(word in marker for word in (
            "secondary",
            "secondarygenre",
            "subgenre",
            "sub-genre",
        )):
            return True

        size_match = re.search(r"font-size:([0-9.]+)(px|em|rem|%)", style)

        if size_match:
            value = float(size_match.group(1))
            unit = size_match.group(2)
            if (
                (unit == "px" and value <= 12)
                or (unit in {"em", "rem"} and value < 0.95)
                or (unit == "%" and value < 95)
            ):
                return True

        if node is row:
            break
        node = node.parent

    return False


def _extract_aoty_user_score(soup: BeautifulSoup) -> str | None:
    # Prefer semantically nearby text around "User Score" so we don't
    # accidentally pick a critic score elsewhere on the page.
    for string in soup.find_all(string=True):
        normalized = " ".join(str(string).split()).casefold()

        if normalized != "user score":
            continue

        checked = 0
        for next_string in string.parent.find_all_next(string=True):
            text = " ".join(str(next_string).split())

            if not text or text.casefold() == "user score":
                continue

            match = re.fullmatch(r"(100|\d{1,2})", text)
            if match:
                return match.group(1)

            checked += 1
            if checked >= 20:
                break

    page_text = " ".join(soup.get_text(" ", strip=True).split())
    match = re.search(
        r"User\s*Score\s*(100|\d{1,2})(?!\d)",
        page_text,
        flags=re.IGNORECASE,
    )
    return match.group(1) if match else None


def _normalize_count(value: str | None) -> str | None:
    """Normalize the AOTY count while preserving commas / K / M."""
    if not value:
        return None

    value = str(value).replace("\xa0", " ")
    value = re.sub(r"\s+", "", value)

    return value or None


def _extract_ratings_count(soup: BeautifulSoup) -> str | None:
    """Read ONLY the count belonging to the release's User Score.

    AOTY currently renders the section in the form:

        User Score
        61
        Based on 574 ratings

    The previous broad fallback could accidentally pick another number from
    the page (for example a song/user count).  This version anchors the search
    to ``User Score`` and requires the explicit ``Based on ... ratings`` text.
    """

    count_pattern = re.compile(
        r"\bBased\s+on\s+"
        r"([\d][\d,.]*(?:\s*[KM])?)"
        r"\s+ratings\b",
        flags=re.IGNORECASE,
    )

    # ------------------------------------------------------------------
    # 1. Exact User Score marker -> nearby strings.
    #
    # Works both for:
    #   "Based on 574 ratings"
    # and split DOM:
    #   "Based on" + "574" + "ratings"
    # ------------------------------------------------------------------
    for string in soup.find_all(string=True):
        if " ".join(str(string).split()).casefold() != "user score":
            continue

        parts = []
        checked = 0

        for next_string in string.parent.find_all_next(string=True):
            value = " ".join(
                str(next_string)
                .replace("\xa0", " ")
                .split()
            )

            if not value:
                continue

            # Another score block means we have left the User Score area.
            if (
                checked > 0
                and value.casefold() in {
                    "critic score",
                    "track ratings",
                    "popular user reviews",
                    "user reviews",
                }
            ):
                break

            parts.append(value)

            # Only a small rolling window close to User Score is accepted.
            neighborhood = " ".join(parts[-10:])
            match = count_pattern.search(neighborhood)

            if match:
                return _normalize_count(match.group(1))

            checked += 1
            if checked >= 28:
                break

    # ------------------------------------------------------------------
    # 2. Closest ancestors of the User Score marker.
    #
    # This catches layouts where the count is hidden in nested spans but
    # still belongs to one score component.
    # ------------------------------------------------------------------
    for string in soup.find_all(string=True):
        if " ".join(str(string).split()).casefold() != "user score":
            continue

        node = string.parent

        for _ in range(5):
            if node is None:
                break

            node_text = " ".join(
                node.get_text(" ", strip=True)
                .replace("\xa0", " ")
                .split()
            )

            # Do not accept an ancestor that clearly contains another score
            # section too — that container is already too broad.
            lower = node_text.casefold()
            if "critic score" not in lower or "user score" not in lower:
                match = count_pattern.search(node_text)
                if match:
                    return _normalize_count(match.group(1))

            # Accessibility attributes sometimes contain "574 ratings".
            for element in node.find_all(True, limit=80):
                attrs = getattr(element, "attrs", {}) or {}

                for attr_name in (
                    "aria-label",
                    "title",
                    "data-title",
                    "data-label",
                ):
                    attr_value = " ".join(
                        str(attrs.get(attr_name) or "")
                        .replace("\xa0", " ")
                        .split()
                    )

                    # Attribute fallback still needs explicit ratings wording.
                    attr_match = re.search(
                        r"\b([\d][\d,.]*(?:\s*[KM])?)\s+ratings\b",
                        attr_value,
                        flags=re.IGNORECASE,
                    )

                    if attr_match:
                        return _normalize_count(attr_match.group(1))

            node = node.parent

    # ------------------------------------------------------------------
    # 3. Strict whole-page fallback.
    #
    # Crucially this still starts at User Score.  There is NO generic
    # "first N ratings anywhere on the page" fallback anymore.
    # ------------------------------------------------------------------
    page_text = " ".join(
        soup.get_text(" ", strip=True)
        .replace("\xa0", " ")
        .split()
    )

    match = re.search(
        r"\bUser\s*Score\b"
        r".{0,120}?"
        r"\bBased\s+on\s+"
        r"([\d][\d,.]*(?:\s*[KM])?)"
        r"\s+ratings\b",
        page_text,
        flags=re.IGNORECASE,
    )

    if match:
        return _normalize_count(match.group(1))

    # ------------------------------------------------------------------
    # 4. Raw HTML fallback, still anchored to User Score.
    # ------------------------------------------------------------------
    source = str(soup).replace("\xa0", " ")

    match = re.search(
        r"User\s*Score"
        r".{0,1800}?"
        r"Based\s+on"
        r".{0,500}?"
        r"([\d][\d,.]*(?:\s*[KM])?)"
        r"(?:&nbsp;|\s|<[^>]+>)*ratings",
        source,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if match:
        return _normalize_count(match.group(1))

    return None


def _extract_tracklist(soup: BeautifulSoup) -> list[dict]:
    heading = None

    for candidate in soup.find_all(["h2", "h3"]):
        heading_text = " ".join(candidate.get_text(" ", strip=True).split()).casefold()
        if heading_text == "track list":
            heading = candidate
            break

    if not heading:
        return []

    tracks = []
    seen = set()
    current_disc = None

    for element in heading.next_elements:
        if (
            element is not heading
            and getattr(element, "name", None) in {"h2", "h3"}
        ):
            break

        if isinstance(element, str):
            text = " ".join(str(element).split())
            if re.fullmatch(r"disc\s+\d+", text, flags=re.IGNORECASE):
                current_disc = text
            continue

        if getattr(element, "name", None) != "a":
            continue

        href = element.get("href", "")
        if "/song/" not in href:
            continue

        title = element.get_text(" ", strip=True)
        if not title:
            continue

        song_url = urljoin(BASE_URL, href)
        if song_url in seen:
            continue
        seen.add(song_url)

        node = element.parent
        row = node

        for _ in range(7):
            if not node:
                break
            song_links = node.select('a[href*="/song/"]')
            if len(song_links) > 1:
                break
            row = node
            node = node.parent

        row_text = " ".join(row.get_text(" ", strip=True).split()) if row else title
        number_match = re.match(r"^\s*(\d{1,3})\b", row_text)
        duration_match = re.search(r"\b\d{1,2}:\d{2}\b", row_text)

        number = int(number_match.group(1)) if number_match else len(tracks) + 1
        duration = duration_match.group(0) if duration_match else None

        track_user_score = None
        if duration_match:
            after_duration = row_text[duration_match.end():]
            score_match = re.search(r"\b(100|\d{1,2})\b\s*$", after_duration)
            if score_match:
                track_user_score = score_match.group(1)

        tracks.append({
            "number": number,
            "title": title,
            "duration": duration,
            "disc": current_disc,
            "url": song_url,
            "user_score": track_user_score,
        })

    return tracks


def get_album_details(album_url: str) -> dict:
    soup = BeautifulSoup(fetch_page(album_url), "html.parser")
    page_text = " ".join(soup.get_text(" ", strip=True).split())

    user_score = _extract_aoty_user_score(soup)
    ratings_count = _extract_ratings_count(soup)

    release_row = _find_details_row(soup, "release date")
    release_date = _details_value_text(release_row, "release date")
    year = None

    if release_date:
        match = re.search(r"\b(?:19|20)\d{2}\b", release_date)
        if match:
            year = match.group(0)

    format_row = _find_details_row(soup, "format")
    album_format = _details_value_text(format_row, "format")

    label_row = _find_details_row(soup, "label")
    labels = []

    if label_row:
        for link in label_row.select('a[href*="/label/"]'):
            name = link.get_text(" ", strip=True)
            if name and name not in labels:
                labels.append(name)

    if not labels:
        fallback_label = _details_value_text(label_row, "label")
        if fallback_label:
            labels.append(fallback_label)

    label = labels[0] if labels else None
    labels_text = ", ".join(labels) if labels else None

    genre_row = _find_details_row(soup, "genre")
    genres: list[str] = []
    secondary_genres: list[str] = []

    if genre_row:
        passed_visual_break = False
        found_primary = False

        for element in genre_row.descendants:
            if getattr(element, "name", None) == "br":
                if found_primary:
                    passed_visual_break = True
                continue

            if getattr(element, "name", None) != "a":
                continue

            if "/genre/" not in element.get("href", ""):
                continue

            genre = element.get_text(" ", strip=True)
            if not genre:
                continue

            is_secondary = passed_visual_break or _is_secondary_genre_link(element, genre_row)

            if is_secondary:
                if genre not in secondary_genres:
                    secondary_genres.append(genre)
            else:
                if genre not in genres:
                    genres.append(genre)
                    found_primary = True

    genres_text = ", ".join(genres) if genres else None

    # Keep the exact compact Markdown used by the current bot.
    secondary_genres_text = (
        f"-# **{', '.join(secondary_genres)}**"
        if secondary_genres
        else None
    )

    vibe_row = _find_details_row(soup, "vibe")
    vibes = []

    if vibe_row:
        for link in vibe_row.select("a[href]"):
            if "/vibe/" not in link.get("href", ""):
                continue
            vibe = link.get_text(" ", strip=True)
            if vibe and vibe not in vibes:
                vibes.append(vibe)

    # Keep the exact compact Markdown used by the current bot.
    vibes_text = (
        f"-# *{', '.join(vibes)}*"
        if vibes
        else None
    )

    ranking_year = None
    year_ranking = None
    year_ranking_text = None
    ranking_match = re.search(
        r"\b((?:19|20)\d{2})\s+Ratings:\s*#\s*([\d,]+)",
        page_text,
        flags=re.IGNORECASE,
    )

    if ranking_match:
        ranking_year = ranking_match.group(1)
        year_ranking = ranking_match.group(2)
        year_ranking_text = f"#{year_ranking}"

    tracklist = _extract_tracklist(soup)
    tracklist_lines = []
    previous_disc = None

    for track in tracklist:
        disc = track.get("disc")
        if disc and disc != previous_disc:
            tracklist_lines.append(disc)
            previous_disc = disc

        line = f"{track['number']}. {track['title']}"
        if track.get("duration"):
            line += f" — {track['duration']}"
        if track.get("user_score"):
            line += f" — {track['user_score']}"
        tracklist_lines.append(line)

    return {
        "url": album_url,
        "user_score": user_score,
        "ratings_count": ratings_count,
        "release_date": release_date,
        "year": year,
        "album_format": album_format,
        "label": label,
        "labels": labels,
        "labels_text": labels_text,
        "genres": genres,
        "genres_text": genres_text,
        "secondary_genres": secondary_genres,
        "secondary_genres_text": secondary_genres_text,
        "vibes": vibes,
        "vibes_text": vibes_text,
        "ranking_year": ranking_year,
        "year_ranking": year_ranking,
        "year_ranking_text": year_ranking_text,
        "tracklist": tracklist,
        "tracklist_text": "\n".join(tracklist_lines) if tracklist_lines else None,
    }


# ---------------------------------------------------------------------------
# Rating cards / dates / metadata flags
# ---------------------------------------------------------------------------
MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


def format_polish_date(text: str) -> str | None:
    if not text:
        return None

    text = text.strip()
    now = datetime.now()
    lower = text.casefold()

    if lower == "just now":
        return now.strftime("%d.%m.%Y")

    relative_patterns = [
        (r"\b(\d+)\s*m(?:in)?\s*ago\b", "minutes"),
        (r"\b(\d+)\s*h(?:r)?\s*ago\b", "hours"),
        (r"\b(\d+)\s*d(?:ay)?\s*ago\b", "days"),
        (r"\b(\d+)\s*w(?:eek)?\s*ago\b", "weeks"),
    ]

    for pattern, unit in relative_patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue

        value = int(match.group(1))
        result = now - timedelta(**{unit: value})
        return result.strftime("%d.%m.%Y")

    match = re.search(
        r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
        r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?|tember)?|"
        r"Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+(\d{1,2})"
        r"(?:,\s*(\d{4}))?",
        text,
        flags=re.IGNORECASE,
    )

    if not match:
        return None

    month = MONTHS.get(match.group(1).lower())
    day = int(match.group(2))

    if not month:
        return None

    if match.group(3):
        year = int(match.group(3))
    else:
        year = now.year
        try:
            candidate = datetime(year, month, day)
            if candidate > now + timedelta(days=2):
                year -= 1
        except ValueError:
            return None

    try:
        return datetime(year, month, day).strftime("%d.%m.%Y")
    except ValueError:
        return None


def _parse_rating_datetime_for_sort(text: str) -> datetime | None:
    if not text:
        return None

    normalized = " ".join(str(text).split())
    now = datetime.now()
    lower = normalized.casefold()

    if lower == "just now":
        return now

    relative_patterns = [
        (r"\b(\d+)\s*m(?:in)?\s*ago\b", "minutes"),
        (r"\b(\d+)\s*h(?:r)?\s*ago\b", "hours"),
        (r"\b(\d+)\s*d(?:ay)?\s*ago\b", "days"),
        (r"\b(\d+)\s*w(?:eek)?\s*ago\b", "weeks"),
    ]

    for pattern, unit in relative_patterns:
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if match:
            return now - timedelta(**{unit: int(match.group(1))})

    formatted = format_polish_date(normalized)
    if not formatted:
        return None

    try:
        return datetime.strptime(formatted, "%d.%m.%Y")
    except ValueError:
        return None


def extract_score(block) -> str | None:
    selectors = [
        ".ratingBlock .rating",
        ".rating",
        ".userRating",
        "[class*='userRating']",
    ]

    for selector in selectors:
        for element in block.select(selector):
            text = clean_text(element)
            if not text:
                continue
            match = re.fullmatch(r"(100|\d{1,2})", text.strip())
            if match:
                return match.group(1)

    return None


def extract_date(block) -> str:
    selectors = [
        ".ratingText",
        ".date",
        "[class*='date']",
        "[class*='Date']",
    ]

    for selector in selectors:
        for element in block.select(selector):
            text = clean_text(element)
            date = format_polish_date(text) if text else None
            if date:
                return date

    for text in block.stripped_strings:
        date = format_polish_date(text)
        if date:
            return date

    return "Brak danych"


def extract_rating_timestamp(block) -> float:
    for text in block.stripped_strings:
        parsed = _parse_rating_datetime_for_sort(text)
        if parsed:
            return parsed.timestamp()
    return 0.0


def extract_cover(block) -> str | None:
    image = block.select_one("img")

    if not image:
        return None

    cover = image.get("data-src") or image.get("data-lazy-src") or image.get("src")

    if not cover:
        return None
    if cover.startswith("//"):
        return "https:" + cover
    if cover.startswith("/"):
        return urljoin(BASE_URL, cover)
    return cover


def _element_marker(element) -> str:
    """Flatten useful element metadata for feature-icon detection.

    AOTY changes icon classes occasionally, so we include all HTML attributes
    (especially data-* state attributes) instead of relying on one CSS class.
    """
    if not hasattr(element, "get"):
        return ""

    parts = []

    for key, raw_value in (getattr(element, "attrs", {}) or {}).items():
        if isinstance(raw_value, (list, tuple)):
            raw_value = " ".join(str(part) for part in raw_value)
        parts.append(f"{key}={raw_value}")

    try:
        text = element.get_text(" ", strip=True)
    except Exception:
        text = ""

    if text and len(text) <= 120:
        parts.append(text)

    return " ".join(parts).casefold()


def _extract_rating_markers(block, album_id: str) -> dict:
    """Read review / track-rating / like badges from a rating card.

    Detection is deliberately conservative.  In particular, a generic heart
    icon or a generic ``/user/.../album/...`` link does *not* prove that a
    release is liked/reviewed.  The old approach produced false positives on
    the grey action icons visible on AOTY rating cards.
    """
    has_review = False
    has_track_ratings = False
    liked = False
    review_url = None

    for element in block.find_all(["a", "button", "i", "span", "div"]):
        marker = _element_marker(element)
        href = str(element.get("href", "")) if hasattr(element, "get") else ""

        # Keep the user-release URL as a useful target, but do not infer a
        # review from the link alone — AOTY can expose that target for ratings
        # without review text as well.
        if "/user/" in href and "/album/" in href and str(album_id) in href:
            review_url = urljoin(BASE_URL, href)

        if any(token in marker for token in (
            "reviewlink",
            "review-link",
            "hasreview",
            "has-review",
            "data-review=true",
            "data-review=1",
            "data-has-review=true",
            "data-has-review=1",
            "fa-pen",
            "fa-pencil",
            "fa-comment",
            "review-icon",
            "review icon",
        )):
            has_review = True

        if any(token in marker for token in (
            "trackrating",
            "track-rating",
            "trackratings",
            "track-ratings",
            "data-track-ratings=true",
            "data-track-ratings=1",
            "data-has-track-ratings=true",
            "data-has-track-ratings=1",
            "fa-list-ol",
            "fa-list-alt",
            "trackscore",
            "track-score",
        )):
            has_track_ratings = True

        # A bare ``fa-heart`` is the grey clickable action visible even when
        # not liked.  Require an explicit active/liked state.
        explicit_like = any(token in marker for token in (
            "data-liked=true",
            "data-liked=1",
            "data-like=true",
            "data-like=1",
            "data-is-liked=true",
            "data-is-liked=1",
            "is-liked",
            "user-liked",
            "album-liked",
            "class=liked",
            " liked active",
            "active liked",
        ))

        if explicit_like and not any(token in marker for token in (
            "unliked",
            "not-liked",
            "data-liked=false",
            "data-liked=0",
            "inactive",
        )):
            liked = True

    return {
        "has_review": has_review,
        "has_track_ratings": has_track_ratings,
        "liked": liked,
        "review_url": review_url,
    }
def parse_album_block(block) -> dict | None:
    album_title_element = block.select_one(".albumTitle")
    album_link = None

    if album_title_element:
        if album_title_element.name == "a" and album_title_element.get("href"):
            album_link = album_title_element
        else:
            album_link = album_title_element.select_one('a[href*="/album/"]')

    if not album_link:
        links = block.select('a[href*="/album/"]')
        for link in links:
            if clean_text(link):
                album_link = link
                break
        if not album_link and links:
            album_link = links[0]

    if not album_link:
        return None

    href = album_link.get("href", "")
    album_id = extract_album_id(href)

    if not album_id:
        return None

    album = clean_text(album_title_element) if album_title_element else None
    if not album:
        album = clean_text(album_link)

    artist = None
    artist_element = block.select_one(".artistTitle")
    if artist_element:
        artist = clean_text(artist_element)
    if not artist:
        artist = clean_text(block.select_one('a[href*="/artist/"]'))

    score = extract_score(block)
    if score is None:
        return None

    date = extract_date(block)
    if date == "Brak danych":
        date = datetime.now().strftime("%d.%m.%Y")

    cover = extract_cover(block)
    release_format = _extract_release_format(block.get_text(" ", strip=True))
    sort_timestamp = extract_rating_timestamp(block)
    album_url = urljoin(BASE_URL, href)
    markers = _extract_rating_markers(block, album_id)

    return {
        "album_id": album_id,
        "artist": artist or "Nieznany artysta",
        "album": album or f"Album #{album_id}",
        "score": score,
        "date": date,
        "url": album_url,
        "cover": cover,
        "release_format": release_format,
        "sort_timestamp": sort_timestamp,
        **markers,
    }


def parse_generic(soup: BeautifulSoup) -> list[dict]:
    results = {}

    for link in soup.select('a[href*="/album/"]'):
        album_id = extract_album_id(link.get("href", ""))

        if not album_id or album_id in results:
            continue

        container = link

        for _ in range(10):
            container = container.parent
            if not container:
                break

            album_ids = {
                found
                for album_link in container.select('a[href*="/album/"]')
                if (found := extract_album_id(album_link.get("href", "")))
            }

            if len(album_ids) > 1:
                break

            if not extract_score(container):
                continue

            item = parse_album_block(container)
            if item and item["album_id"] == album_id:
                results[album_id] = item
                break

    return list(results.values())


def _parse_ratings_soup(soup: BeautifulSoup, forced_format: str | None = None) -> list[dict]:
    results = {}

    for block in soup.select(".albumBlock"):
        item = parse_album_block(block)
        if not item:
            continue
        if forced_format:
            item["release_format"] = forced_format
        results[item["album_id"]] = item

    for item in parse_generic(soup):
        if forced_format:
            item["release_format"] = forced_format
        results.setdefault(item["album_id"], item)

    page_ratings = []
    added = set()

    for link in soup.select('a[href*="/album/"]'):
        album_id = extract_album_id(link.get("href", ""))
        if not album_id or album_id not in results or album_id in added:
            continue
        page_ratings.append(results[album_id])
        added.add(album_id)

    return page_ratings


def _ratings_route_url(username: str, slug: str | None = None, page: int = 1) -> str:
    base = f"{BASE_URL}/user/{username}/ratings/"
    if slug:
        base += f"{slug}/"
    if page > 1:
        base += f"{page}/"
    return base


def _get_ratings_from_route(
    username: str,
    slug: str | None = None,
    limit: int = 60,
    forced_format: str | None = None,
) -> list[dict]:
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 60

    if limit <= 0:
        return []

    all_ratings = []
    seen = set()
    page = 1

    while len(all_ratings) < limit and page <= 100:
        url = _ratings_route_url(username, slug=slug, page=page)
        soup = BeautifulSoup(fetch_page(url), "html.parser")
        page_ratings = _parse_ratings_soup(soup, forced_format=forced_format)

        if not page_ratings:
            break

        new_on_page = 0

        for item in page_ratings:
            album_id = item["album_id"]
            if album_id in seen:
                continue
            seen.add(album_id)
            all_ratings.append(item)
            new_on_page += 1
            if len(all_ratings) >= limit:
                break

        if new_on_page == 0:
            break
        page += 1

    return all_ratings[:limit]


def get_ratings_for_format(username: str, format_key: str, limit: int | None = None) -> list[dict]:
    info = RATING_FORMATS.get(str(format_key))

    if not info:
        return []

    if limit is None:
        limit = RATING_FETCH_LIMITS.get(format_key, 0)

    return _get_ratings_from_route(
        username=username,
        slug=info["slug"],
        limit=limit,
        forced_format=info["label"],
    )


def _merge_rating_lists(*rating_lists: list[dict]) -> list[dict]:
    merged = {}
    sequence = 0

    for ratings in rating_lists:
        for item in ratings:
            album_id = str(item.get("album_id", ""))
            if not album_id:
                continue

            sequence += 1
            candidate = dict(item)
            candidate["_merge_sequence"] = sequence
            current = merged.get(album_id)

            if current is None:
                merged[album_id] = candidate
                continue

            if not current.get("release_format") and candidate.get("release_format"):
                current["release_format"] = candidate["release_format"]

            # Merge feature flags even if the older candidate wins timestamp.
            for key in ("has_review", "has_track_ratings", "liked"):
                current[key] = bool(current.get(key) or candidate.get(key))

            if not current.get("review_url") and candidate.get("review_url"):
                current["review_url"] = candidate["review_url"]

            if float(candidate.get("sort_timestamp") or 0) > float(current.get("sort_timestamp") or 0):
                candidate["_merge_sequence"] = current.get("_merge_sequence", sequence)
                for key in ("has_review", "has_track_ratings", "liked"):
                    candidate[key] = bool(candidate.get(key) or current.get(key))
                if not candidate.get("review_url"):
                    candidate["review_url"] = current.get("review_url")
                merged[album_id] = candidate

    items = list(merged.values())
    items.sort(
        key=lambda item: (
            float(item.get("sort_timestamp") or 0),
            -int(item.get("_merge_sequence") or 0),
        ),
        reverse=True,
    )

    for item in items:
        item.pop("_merge_sequence", None)

    return items


def get_ratings(username: str, max_pages=None, fetch_limits: dict | None = None) -> list[dict]:
    limits = dict(RATING_FETCH_LIMITS if fetch_limits is None else fetch_limits)
    rating_lists = []

    for format_key, info in RATING_FORMATS.items():
        raw_limit = limits.get(format_key, limits.get(info["slug"], 0))
        try:
            limit = max(0, int(raw_limit))
        except (TypeError, ValueError):
            limit = 0

        if limit <= 0:
            continue

        rating_lists.append(get_ratings_for_format(username, format_key, limit))

    return _merge_rating_lists(*rating_lists)


def get_recent_ratings(
    username: str,
    count: int = 20,
    format_key: str = "all",
) -> list[dict]:
    try:
        count = max(1, min(50, int(count)))
    except (TypeError, ValueError):
        count = 20

    if format_key and format_key != "all":
        return get_ratings_for_format(username, format_key, count)[:count]

    # The root ratings route contains album-like formats. Single and Music
    # Video live behind their own AOTY filters, so merge those separately.
    album_like = _get_ratings_from_route(username, slug=None, limit=count)
    singles = _get_ratings_from_route(
        username,
        slug="single",
        limit=count,
        forced_format="Single",
    )
    music_videos = _get_ratings_from_route(
        username,
        slug="music-video",
        limit=count,
        forced_format="Music Video",
    )

    return _merge_rating_lists(album_like, singles, music_videos)[:count]


# ---------------------------------------------------------------------------
# Per-user release page: review, track ratings, like
# ---------------------------------------------------------------------------

def _fetch_user_release_page(
    username: str,
    album_id: str,
    album_url: str | None,
    user_release_url: str | None = None,
):
    """Fetch the canonical /user/<name>/album/<release>/ page.

    The public album URL and the user-rating URL do NOT use the same slug.
    Example:
        /album/1225702-kiiikiii-uncut-gem.php
    vs:
        /user/<name>/album/1225702-uncut-gem/

    Therefore the exact URL captured from the user's rating card is preferred.
    """

    username = str(username).strip()
    album_id = str(album_id).strip()

    candidates = []

    def add_candidate(url):
        if not url:
            return

        absolute = urljoin(BASE_URL, str(url).strip())

        if absolute not in candidates:
            candidates.append(absolute)

    # Best source: exact /user/.../album/... href from AOTY itself.
    add_candidate(user_release_url)

    # AOTY may redirect this short ID-only route to its canonical user URL.
    add_candidate(
        f"{BASE_URL}/user/{username}/album/{album_id}/"
    )

    # Keep the old derived URL only as a final compatibility fallback.
    if album_url and "/album/" in str(album_url):
        release_path = str(album_url).split("/album/", 1)[1].strip("/")
        if release_path:
            release_path = re.sub(
                r"\.php$",
                "",
                release_path,
                flags=re.IGNORECASE,
            )
            add_candidate(
                f"{BASE_URL}/user/{username}/album/{release_path}/"
            )

    last_url = None

    for candidate in candidates:
        last_url = candidate

        response = session.get(
            candidate,
            timeout=30,
        )

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            message = "HTTP 429 - za dużo zapytań"
            if retry_after:
                message += f" (Retry-After: {retry_after}s)"
            raise AOTYRateLimit(message)

        if response.status_code == 404:
            continue

        response.raise_for_status()

        final_url = response.url
        final_lower = final_url.casefold()
        required_user = f"/user/{username.casefold()}/album/"

        # AOTY sometimes redirects an invalid user-album URL to a generic
        # album/profile page. Accept only the correct user's album page.
        if required_user not in final_lower:
            continue

        if str(album_id) not in final_lower:
            continue

        return BeautifulSoup(
            response.text,
            "html.parser",
        ), final_url

    return None, last_url


def _extract_user_score_from_user_release_page(soup: BeautifulSoup, username: str) -> str | None:
    username_normalized = str(username).strip().casefold()

    for string in soup.find_all(string=True):
        text = " ".join(str(string).split())
        if text.casefold() != username_normalized:
            continue

        checked = 0
        for next_string in string.parent.find_all_next(string=True):
            candidate = " ".join(str(next_string).split())
            if not candidate:
                continue
            match = re.fullmatch(r"(100|\d{1,2})", candidate)
            if match:
                return match.group(1)
            checked += 1
            if checked >= 35:
                break

    return extract_score(soup)


def _extract_review_text(soup: BeautifulSoup) -> str | None:
    selectors = [
        ".userReviewText",
        ".reviewText",
        ".reviewBody",
        ".reviewContent",
        ".albumReviewText",
        "[class*='userReviewText']",
        "[class*='reviewText']",
        "[class*='reviewBody']",
        "[class*='reviewContent']",
    ]

    candidates = []

    for selector in selectors:
        for element in soup.select(selector):
            text = "\n".join(
                line.strip()
                for line in element.get_text("\n", strip=True).splitlines()
                if line.strip()
            )
            if 3 <= len(text) <= 12000:
                candidates.append(text)

    if candidates:
        # A review body should usually be the largest meaningful candidate.
        return max(candidates, key=len)

    # Conservative fallback: use content between the score/date area and the
    # first well-known section marker. We only accept a substantial paragraph.
    page_lines = [
        " ".join(str(line).split())
        for line in soup.stripped_strings
        if " ".join(str(line).split())
    ]

    stop_words = {
        "track ratings",
        "play this on",
        "comments",
        "amazon",
        "apple music",
        "spotify",
        "vinyl",
    }

    score_index = None
    for index, line in enumerate(page_lines):
        if re.fullmatch(r"(100|\d{1,2})", line):
            score_index = index
            break

    if score_index is None:
        return None

    body = []
    for line in page_lines[score_index + 1:]:
        if line.casefold() in stop_words:
            break
        if re.fullmatch(r"\d+[dwmhy](?:\*)?", line.casefold()):
            continue
        if re.fullmatch(r"[\d,]+", line):
            continue
        body.append(line)

    text = "\n".join(body).strip()
    return text if len(text) >= 12 else None


def _track_title_key(text: str | None) -> str:
    """Normalize a track title only for matching user ratings to the tracklist."""
    if not text:
        return ""

    value = str(text).casefold()

    return "".join(
        char
        for char in value
        if char.isalnum()
    )


def _extract_user_track_ratings(soup: BeautifulSoup) -> list[dict]:
    """Extract every track score that the user actually entered.

    AOTY may show only the tracks that were rated in the "Track Ratings"
    section. A partial set is valid and must not be treated as empty.
    """
    marker = None

    for string in soup.find_all(string=True):
        if " ".join(str(string).split()).casefold() == "track ratings":
            marker = string.parent
            break

    if marker is None:
        return []

    results: list[dict] = []
    seen: set[tuple[int, str]] = set()

    def add_result(number, title, score):
        try:
            number = int(number)
        except (TypeError, ValueError):
            return

        title = " ".join(str(title or "").split()).strip(" -–—/|")
        score = str(score or "").strip()

        if not title:
            return

        if not re.fullmatch(r"(100|\d{1,2})", score):
            return

        if re.fullmatch(r"\d{1,2}:\d{2}", title):
            return

        key = (number, _track_title_key(title))

        if key in seen:
            return

        seen.add(key)
        results.append({
            "number": number,
            "title": title,
            "score": score,
        })

    # 1) Row-like DOM elements.
    for element in marker.find_all_next(
        ["tr", "li", "div", "p"],
        limit=350,
    ):
        row_text = " ".join(
            element.get_text(
                " ",
                strip=True,
            ).split()
        )

        lower = row_text.casefold()

        if lower in {
            "play this on",
            "comments",
        }:
            break

        if not row_text or len(row_text) > 500:
            continue

        # Typical flattened AOTY row:
        #   5 | Take Me Thru Dere / 58
        match = re.match(
            r"^\s*(\d{1,3})\s*"
            r"(?:[.|:)\-–—|]\s*)?"
            r"(.+?)\s*"
            r"(?:/|—|–|\|)\s*"
            r"(100|\d{1,2})\s*$",
            row_text,
        )

        if match:
            add_result(
                match.group(1),
                match.group(2),
                match.group(3),
            )
            continue

        # Some layouts lose separators in get_text():
        #   5 Take Me Thru Dere 58
        match = re.match(
            r"^\s*(\d{1,3})\s+"
            r"(.+?)\s+"
            r"(100|\d{1,2})\s*$",
            row_text,
        )

        if match:
            add_result(
                match.group(1),
                match.group(2),
                match.group(3),
            )

    # 2) Token fallback for rows split across spans.
    tokens = []

    for string in marker.find_all_next(string=True):
        token = " ".join(str(string).split())

        if not token:
            continue

        lower = token.casefold()

        if lower in {
            "play this on",
            "comments",
            "amazon",
            "apple music",
            "spotify",
        }:
            break

        tokens.append(token)

        if len(tokens) >= 1200:
            break

    i = 0

    while i < len(tokens):
        number_match = re.fullmatch(
            r"(\d{1,3})",
            tokens[i],
        )

        if not number_match:
            i += 1
            continue

        number = number_match.group(1)
        slash_index = None

        for j in range(
            i + 1,
            min(len(tokens), i + 18),
        ):
            if tokens[j] in {"/", "—", "–"}:
                slash_index = j
                break

        if slash_index is None:
            i += 1
            continue

        score_index = None

        for j in range(
            slash_index + 1,
            min(len(tokens), slash_index + 5),
        ):
            if re.fullmatch(
                r"(100|\d{1,2})",
                tokens[j],
            ):
                score_index = j
                break

        if score_index is None:
            i += 1
            continue

        title_parts = [
            token
            for token in tokens[
                i + 1:slash_index
            ]
            if token not in {
                "|",
                "•",
                "-",
                "–",
                "—",
            }
        ]

        title = " ".join(
            title_parts
        ).strip()

        if title:
            add_result(
                number,
                title,
                tokens[score_index],
            )

        i = score_index + 1

    results.sort(
        key=lambda item: (
            int(item.get("number") or 9999),
            _track_title_key(item.get("title")),
        )
    )

    return results


def _merge_partial_track_ratings(
    album_tracklist: list[dict],
    user_track_ratings: list[dict],
) -> list[dict]:
    """Overlay partial user scores on the complete release tracklist.

    Unrated tracks remain in the returned list with score=None.
    The Discord view already renders score=None as NR.
    """
    album_tracklist = list(
        album_tracklist
        or []
    )

    user_track_ratings = list(
        user_track_ratings
        or []
    )

    if not album_tracklist:
        return user_track_ratings

    by_number: dict[int, dict] = {}
    by_title: dict[str, dict] = {}

    for rating in user_track_ratings:
        number = rating.get("number")

        try:
            number = int(number)
        except (TypeError, ValueError):
            number = None

        if number is not None:
            by_number[number] = rating

        title_key = _track_title_key(
            rating.get("title")
        )

        if title_key:
            by_title[title_key] = rating

    merged = []
    used_rating_ids = set()

    for fallback_index, track in enumerate(
        album_tracklist,
        start=1,
    ):
        number = track.get("number")

        try:
            number = int(number)
        except (TypeError, ValueError):
            number = fallback_index

        title = (
            track.get("title")
            or f"Track {number}"
        )

        title_key = _track_title_key(
            title
        )

        rating = by_number.get(
            number
        )

        # Number is normally enough. Title fallback protects odd AOTY
        # numbering on multi-disc / bonus-track releases.
        if rating is None and title_key:
            rating = by_title.get(
                title_key
            )

        if rating is not None:
            used_rating_ids.add(
                id(rating)
            )

        merged.append({
            "number": number,
            "title": title,
            "score": (
                rating.get("score")
                if rating
                else None
            ),
            "duration": track.get("duration"),
            "disc": track.get("disc"),
            "url": track.get("url"),
        })

    # Keep unusual rated bonus/hidden tracks even if the public tracklist
    # omitted them.
    for rating in user_track_ratings:
        if id(rating) in used_rating_ids:
            continue

        merged.append({
            "number": rating.get("number"),
            "title": rating.get("title") or "Nieznany utwór",
            "score": rating.get("score"),
            "duration": None,
            "disc": None,
            "url": None,
        })

    return merged


def _complete_user_track_ratings(
    album_url: str | None,
    user_track_ratings: list[dict],
) -> list[dict]:
    """Return the full tracklist with the user's partial scores overlaid."""
    if not album_url:
        return list(
            user_track_ratings
            or []
        )

    try:
        details = get_album_details(
            album_url
        )

        return _merge_partial_track_ratings(
            details.get("tracklist") or [],
            user_track_ratings,
        )

    except AOTYRateLimit:
        raise

    except Exception:
        # A failed second request must not hide ratings already parsed.
        return list(
            user_track_ratings
            or []
        )


def _detect_user_like(soup: BeautifulSoup) -> bool:
    """Detect an explicit liked state; ignore neutral/grey heart controls."""
    for element in soup.find_all(["a", "button", "i", "span", "div"]):
        marker = _element_marker(element)

        positive = any(token in marker for token in (
            "data-liked=true",
            "data-liked=1",
            "data-like=true",
            "data-like=1",
            "data-is-liked=true",
            "data-is-liked=1",
            "is-liked",
            "user-liked",
            "album-liked",
            "class=liked",
            " liked active",
            "active liked",
        ))

        negative = any(token in marker for token in (
            "unliked",
            "not-liked",
            "data-liked=false",
            "data-liked=0",
            "inactive",
        ))

        if positive and not negative:
            return True

    return False
def get_user_rating_for_album(
    username: str,
    album_id: str,
    album_url: str | None = None,
    release_format: str | None = None,
    fallback_limit: int | None = None,
    user_release_url: str | None = None,
) -> dict:
    """Fetch one user's live score + review + track ratings + like state.

    ``user_release_url`` is optional and preserves backwards compatibility.
    When available it should be the exact /user/.../album/... URL captured
    from the rating card.
    """

    username = str(username).strip()
    album_id = str(album_id).strip()

    if fallback_limit is None:
        fallback_limit = ALBUM_LOOKUP_FALLBACK_LIMIT

    def parse_user_page(
        soup: BeautifulSoup,
        resolved_user_url: str | None,
    ) -> dict:
        score = _extract_user_score_from_user_release_page(
            soup,
            username,
        )

        review_text = _extract_review_text(
            soup
        )

        partial_track_ratings = _extract_user_track_ratings(
            soup
        )

        # A partial set is VALID. Expand against the complete public
        # tracklist so every unrated song is returned with score=None -> NR.
        track_ratings = _complete_user_track_ratings(
            album_url,
            partial_track_ratings,
        )

        return {
            "score": str(score) if score is not None else None,
            "date": extract_date(soup),
            "source": "AOTY live",
            "review_url": resolved_user_url,
            "review_text": review_text,
            "has_review": bool(review_text),
            "track_ratings": track_ratings,

            # True if at least ONE real track score exists.
            "has_track_ratings": bool(
                partial_track_ratings
            ),

            "liked": _detect_user_like(soup),
        }

    # ------------------------------------------------------------------
    # 1. Direct user-release page.
    # ------------------------------------------------------------------
    try:
        soup, resolved_user_url = _fetch_user_release_page(
            username,
            album_id,
            album_url,
            user_release_url=user_release_url,
        )

        if soup is not None:
            return parse_user_page(
                soup,
                resolved_user_url,
            )

    except AOTYRateLimit:
        raise
    except requests.RequestException:
        pass
    except Exception:
        pass

    # ------------------------------------------------------------------
    # 2. Find the release on the user's ratings list.
    #
    # Apart from the score this gives us AOTY's exact canonical
    # /user/.../album/... link. Once found, fetch that page and parse the
    # real review/track ratings instead of returning flags only.
    # ------------------------------------------------------------------
    format_key = _format_key_from_label(
        release_format
    )

    if format_key:
        ratings = get_ratings_for_format(
            username,
            format_key,
            fallback_limit,
        )
    else:
        ratings = _get_ratings_from_route(
            username,
            slug=None,
            limit=fallback_limit,
        )

    for item in ratings:
        if str(item.get("album_id")) != album_id:
            continue

        exact_user_url = (
            item.get("review_url")
            or user_release_url
        )

        if exact_user_url:
            try:
                soup, resolved_user_url = _fetch_user_release_page(
                    username,
                    album_id,
                    album_url,
                    user_release_url=exact_user_url,
                )

                if soup is not None:
                    result = parse_user_page(
                        soup,
                        resolved_user_url,
                    )

                    # Preserve the list score/date if the detail page parser
                    # somehow cannot read those two fields.
                    if result.get("score") is None:
                        result["score"] = (
                            str(item.get("score", ""))
                            or None
                        )

                    if not result.get("date"):
                        result["date"] = item.get("date")

                    return result

            except AOTYRateLimit:
                raise
            except Exception:
                pass

        # Last fallback: the list can still tell us score + flags.
        return {
            "score": str(item.get("score", "")) or None,
            "date": item.get("date"),
            "source": "AOTY live",
            "review_url": exact_user_url,
            "review_text": None,
            "has_review": bool(item.get("has_review")),
            "track_ratings": [],
            "has_track_ratings": bool(
                item.get("has_track_ratings")
            ),
            "liked": bool(item.get("liked")),
        }

    return {
        "score": None,
        "date": None,
        "source": "AOTY live",
        "review_url": user_release_url,
        "review_text": None,
        "has_review": False,
        "track_ratings": [],
        "has_track_ratings": False,
        "liked": False,
    }


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------

def _profile_count(page_text: str, label: str) -> str | None:
    patterns = [
        rf"\b([\d,]+)\s*{re.escape(label)}\b",
        rf"\b{re.escape(label)}\s*\(([\d,]+)\)",
    ]

    for pattern in patterns:
        match = re.search(pattern, page_text, flags=re.IGNORECASE)
        if match:
            return match.group(1)

    return None


def _extract_profile_avatar(soup: BeautifulSoup) -> str | None:
    for image in soup.find_all("img"):
        src = image.get("data-src") or image.get("src") or ""
        if "/user/thumbs/" not in src or src.endswith("/default.jpg"):
            continue
        if src.startswith("//"):
            return "https:" + src
        if src.startswith("/"):
            return urljoin(BASE_URL, src)
        return src
    return None


def _find_exact_text_marker(soup: BeautifulSoup, wanted: str):
    wanted = wanted.casefold()

    for string in soup.find_all(string=True):
        if " ".join(str(string).split()).casefold() == wanted:
            return string
    return None


def _detect_profile_favorite_kind(soup: BeautifulSoup) -> str | None:
    marker = _find_exact_text_marker(soup, "favorites")
    if marker is None:
        return None

    checked = 0
    for string in marker.parent.find_all_next(string=True):
        text = " ".join(str(string).split()).casefold()
        if not text:
            continue
        if text == "albums":
            return "albums"
        if text == "artists":
            return "artists"
        if text.startswith("best of ") or text == "recently rated":
            break
        checked += 1
        if checked >= 30:
            break
    return None


def _extract_profile_favorites(soup: BeautifulSoup, limit: int = 5) -> tuple[str | None, list[dict]]:
    marker = _find_exact_text_marker(soup, "favorites")
    favorite_kind = _detect_profile_favorite_kind(soup)

    if marker is None or favorite_kind not in {"albums", "artists"}:
        return favorite_kind, []

    favorites = []
    seen = set()

    for element in marker.parent.next_elements:
        if getattr(element, "name", None) in {"h2", "h3"}:
            heading = " ".join(element.get_text(" ", strip=True).split()).casefold()
            if heading.startswith("best of ") or heading == "recently rated":
                break

        if getattr(element, "name", None) != "a":
            continue

        href = element.get("href", "")

        if favorite_kind == "artists":
            if "/artist/" not in href:
                continue
            name = element.get_text(" ", strip=True)
            if not name:
                continue
            url = urljoin(BASE_URL, href)
            key = ("artist", url)
            if key in seen:
                continue
            seen.add(key)
            favorites.append({
                "type": "artist",
                "name": name,
                "artist": name,
                "album": None,
                "url": url,
            })
        else:
            album_id = extract_album_id(href)
            if not album_id or ("album", album_id) in seen:
                continue
            title = element.get_text(" ", strip=True)
            if not title:
                continue
            container = _release_container_for_link(element)
            artist_link = container.select_one('a[href*="/artist/"]') if container else None
            artist = artist_link.get_text(" ", strip=True) if artist_link else None
            seen.add(("album", album_id))
            favorites.append({
                "type": "album",
                "name": title,
                "artist": artist,
                "album": title,
                "url": urljoin(BASE_URL, href),
            })

        if len(favorites) >= limit:
            break

    return favorite_kind, favorites


def _extract_profile_average(soup: BeautifulSoup) -> float | None:
    marker = _find_exact_text_marker(soup, "rating distribution")
    if marker is None:
        return None

    midpoint_map = {
        "100": 100.0,
        "90-99": 94.5,
        "80-89": 84.5,
        "70-79": 74.5,
        "60-69": 64.5,
        "50-59": 54.5,
        "40-49": 44.5,
        "30-39": 34.5,
        "20-29": 24.5,
        "10-19": 14.5,
        "0-9": 4.5,
    }

    found: dict[str, int] = {}

    for row in marker.parent.find_all_next("tr", limit=30):
        cells = [
            " ".join(cell.get_text(" ", strip=True).split())
            for cell in row.find_all(["th", "td"])
        ]
        if not cells:
            continue

        compact = re.sub(r"\s+", "", " ".join(cells))
        label_match = re.search(
            r"(100|90-99|80-89|70-79|60-69|50-59|40-49|30-39|20-29|10-19|0-9)",
            compact,
        )
        if not label_match:
            continue

        label = label_match.group(1)
        count_match = re.search(r"([\d,]+)", compact[label_match.end():])
        if count_match:
            found[label] = int(count_match.group(1).replace(",", ""))
        if len(found) >= 11:
            break

    if not found:
        texts = []
        checked = 0
        for element in marker.parent.next_elements:
            if isinstance(element, str):
                value = " ".join(str(element).split())
                if value:
                    texts.append(value)
            checked += 1
            if checked >= 350:
                break

        distribution_text = " ".join(texts)
        for label in midpoint_map:
            match = re.search(
                rf"(?<!\d){re.escape(label)}\s+([\d,]+)",
                distribution_text,
            )
            if match:
                found[label] = int(match.group(1).replace(",", ""))

    total_count = sum(found.values())
    if total_count <= 0:
        return None

    weighted = sum(midpoint_map[label] * count for label, count in found.items())
    return weighted / total_count


def get_profile_data(username: str, recent_limit: int = 50) -> dict:
    username = str(username).strip()
    url = f"{BASE_URL}/user/{username}/"
    soup = BeautifulSoup(fetch_page(url), "html.parser")

    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if " - profile - album of the year" not in title.casefold():
        raise AOTYUserNotFound()

    heading = soup.find("h1")
    display_username = heading.get_text(" ", strip=True) if heading else username
    page_text = " ".join(soup.get_text(" ", strip=True).split())
    average_rating = _extract_profile_average(soup)
    favorite_kind, favorites = _extract_profile_favorites(soup, limit=5)

    try:
        recent_limit = max(5, min(50, int(recent_limit)))
    except (TypeError, ValueError):
        recent_limit = 50

    # Fetch up to 50 recent ratings so /profile can paginate 10 x 5.
    recent_ratings = get_recent_ratings(username, recent_limit)

    favorite_albums = favorites if favorite_kind == "albums" else []
    favorite_artists = favorites if favorite_kind == "artists" else []

    return {
        "username": display_username,
        "url": url,
        "avatar": _extract_profile_avatar(soup),
        "ratings_count": _profile_count(page_text, "Ratings"),
        "reviews_count": _profile_count(page_text, "Reviews"),
        "lists_count": _profile_count(page_text, "Lists"),
        "following_count": _profile_count(page_text, "Following"),
        "followers_count": _profile_count(page_text, "Followers"),
        "average_rating": average_rating,
        "average_rating_text": f"~{average_rating:.1f}" if average_rating is not None else None,
        "favorite_kind": favorite_kind,
        "favorites": favorites,
        "favorite_albums": favorite_albums,
        "favorite_artists": favorite_artists,
        "recent_ratings": recent_ratings,
    }
