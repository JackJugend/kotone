"""Scraping/search layer for Album of the Year.

No Discord code lives here. Commands and the monitor use the same functions,
so parsing behaviour stays consistent everywhere.
"""

from __future__ import annotations

import difflib
import re
import time
import unicodedata
from html import unescape
from datetime import datetime, timedelta
from urllib.parse import quote_plus, urljoin

import requests
from bs4 import BeautifulSoup

from http_client import HTTP, ExternalRateLimit, ExternalUnavailable
from settings import (
    ALBUM_LOOKUP_FALLBACK_LIMIT,
    AOTY_ARCHIVE_MAX_PAGES,
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

# All AOTY requests go through one shared transport. The parser layer below
# never opens its own Session, so retries/rate limiting/cache cannot diverge
# between commands.
HTTP.configure_headers(HEADERS)


class AOTYRateLimit(Exception):
    pass


class AOTYUserNotFound(Exception):
    pass


class AOTYArchiveIncomplete(Exception):
    """Safety stop: paginacja nie zakończyła się w rozsądnym limicie stron."""




# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def fetch_page(url: str, expected_url: str | None = None) -> str:
    """Fetch one AOTY page through the central resilient transport."""
    try:
        result = HTTP.get(url)
    except ExternalRateLimit as exc:
        raise AOTYRateLimit(str(exc)) from exc
    except ExternalUnavailable as exc:
        raise requests.ConnectionError(str(exc)) from exc

    if expected_url:
        final_url = result.url.rstrip("/").casefold()
        expected = expected_url.rstrip("/").casefold()

        if final_url != expected:
            raise AOTYUserNotFound()

    return result.text


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

_ARTIST_SEARCH_CACHE: dict[str, tuple[float, list[dict]]] = {}
_ARTIST_SEARCH_CACHE_TTL = 90


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


def _artist_search_candidates_from_soup(
    soup: BeautifulSoup,
) -> dict[str, str]:
    """Collect artist profile links from an AOTY search-result page."""
    candidates: dict[str, str] = {}

    canonical = soup.find(
        "link",
        rel=lambda value: (
            value
            and "canonical" in str(value).casefold()
        ),
    )

    if canonical:
        canonical_url = canonical.get(
            "href",
            "",
        )

        if "/artist/" in canonical_url:
            heading = soup.find("h1")
            name = (
                heading.get_text(" ", strip=True)
                if heading
                else ""
            )

            if name:
                candidates[
                    canonical_url
                ] = name

    for link in soup.select(
        'a[href*="/artist/"]'
    ):
        href = link.get(
            "href",
            "",
        )

        match = re.search(
            r"/artist/(\d+(?:-[^/?#]+)?)/?",
            href,
        )

        if not match:
            continue

        name = link.get_text(
            " ",
            strip=True,
        )

        if not name or name.casefold() in {
            "artists",
            "highest rated",
            "random",
            "similar artists",
            "related artists",
        }:
            continue

        artist_url = (
            f"{BASE_URL}/artist/"
            f"{match.group(1)}/"
        )

        candidates.setdefault(
            artist_url,
            name,
        )

    return candidates


def _artist_search_query_variants(
    query: str,
) -> list[str]:
    """Useful query variants for AOTY's search.

    AOTY's canonical display name can differ from an AKA word order, e.g.
    "Sheena Ringo" (AKA) vs "Ringo Sheena" (display name).  Trying the reversed
    two-word form lets us reach the artist page, where the real AKA list is
    then verified.
    """
    query = " ".join(
        str(query or "").split()
    )

    variants = [
        query,
    ]

    parts = query.split()

    if len(parts) == 2:
        reversed_query = (
            f"{parts[1]} {parts[0]}"
        )

        if (
            normalize_match_text(
                reversed_query
            )
            != normalize_match_text(
                query
            )
        ):
            variants.append(
                reversed_query
            )

    return variants


def _artist_search_score(
    query: str,
    name: str,
    akas: list[str],
) -> tuple[float, str | None]:
    """Rank by the best match among canonical name AND AOTY AKAs."""
    normalized_query = normalize_match_text(
        query
    )

    best_score = fuzzy_match_score(
        query,
        name,
    )

    matched_aka = None

    for aka in akas:
        score = fuzzy_match_score(
            query,
            aka,
        )

        normalized_aka = normalize_match_text(
            aka
        )

        # Exact AKA should always beat a merely fuzzy canonical-name match.
        if (
            normalized_query
            and normalized_aka == normalized_query
        ):
            score = max(
                score,
                1.25,
            )

        elif (
            normalized_query
            and normalized_query in normalized_aka
        ):
            score = max(
                score,
                1.05,
            )

        if score > best_score:
            best_score = score
            matched_aka = aka

    return (
        best_score,
        matched_aka,
    )


def search_aoty_artists(
    query: str,
    limit: int = 10,
) -> list[dict]:
    """Search AOTY artists by canonical name OR AOTY Also Known As values."""
    query = str(
        query or ""
    ).strip()

    if not query:
        return []

    direct_url = _artist_direct_value_to_url(
        query
    )

    if direct_url:
        try:
            soup = BeautifulSoup(
                fetch_page(
                    direct_url
                ),
                "html.parser",
            )

            heading = soup.find(
                "h1"
            )

            name = (
                heading.get_text(
                    " ",
                    strip=True,
                )
                if heading
                else query
            )

            metadata = _extract_artist_metadata(
                soup
            )

            return [{
                "name": name,
                "url": direct_url,
                "value": (
                    "aoty_artist:"
                    + direct_url
                    .split(
                        "/artist/",
                        1,
                    )[1]
                    .strip("/")
                ),
                "score": 1.0,
                "akas": metadata.get(
                    "akas",
                    [],
                ),
                "matched_aka": None,
            }]

        except Exception:
            return []

    cache_key = normalize_match_text(
        query
    )

    now = time.monotonic()

    cached = _ARTIST_SEARCH_CACHE.get(
        cache_key
    )

    if (
        cached
        and now - cached[0]
        < _ARTIST_SEARCH_CACHE_TTL
    ):
        return cached[1][
            :max(
                1,
                int(limit),
            )
        ]

    candidates: dict[str, str] = {}

    # --------------------------------------------------------
    # 1. Native artist search + useful name-order variant.
    # --------------------------------------------------------
    for variant in _artist_search_query_variants(
        query
    ):
        search_url = (
            f"{BASE_URL}/search/artists/"
            f"?q={quote_plus(variant)}"
        )

        try:
            soup = BeautifulSoup(
                fetch_page(
                    search_url
                ),
                "html.parser",
            )
        except AOTYRateLimit:
            raise
        except Exception:
            continue

        found = _artist_search_candidates_from_soup(
            soup
        )

        for artist_url, name in found.items():
            candidates.setdefault(
                artist_url,
                name,
            )

    # --------------------------------------------------------
    # 2. Album-search fallback.
    #
    # Some aliases are visible on release pages/search results even when
    # /search/artists does not return the artist for that AKA.
    # --------------------------------------------------------
    if len(candidates) < max(3, int(limit)):
        for variant in _artist_search_query_variants(
            query
        ):
            search_url = (
                f"{BASE_URL}/search/albums/"
                f"?q={quote_plus(variant)}"
            )

            try:
                soup = BeautifulSoup(
                    fetch_page(
                        search_url
                    ),
                    "html.parser",
                )
            except AOTYRateLimit:
                raise
            except Exception:
                continue

            found = _artist_search_candidates_from_soup(
                soup
            )

            for artist_url, name in found.items():
                candidates.setdefault(
                    artist_url,
                    name,
                )

            if len(candidates) >= max(
                6,
                int(limit),
            ):
                break

    # --------------------------------------------------------
    # 3. Verify/rank with the REAL AOTY AKA list from artist profiles.
    #
    # Only inspect a modest number of candidates to avoid hammering AOTY
    # during Discord autocomplete.
    # --------------------------------------------------------
    prelim = sorted(
        candidates.items(),
        key=lambda pair: fuzzy_match_score(
            query,
            pair[1],
        ),
        reverse=True,
    )[:5]

    ranked = []

    for artist_url, fallback_name in prelim:
        name = fallback_name
        akas = []

        try:
            artist_soup = BeautifulSoup(
                fetch_page(
                    artist_url
                ),
                "html.parser",
            )

            heading = artist_soup.find(
                "h1"
            )

            if heading:
                name = heading.get_text(
                    " ",
                    strip=True,
                )

            metadata = _extract_artist_metadata(
                artist_soup
            )

            akas = list(
                metadata.get(
                    "akas",
                    [],
                )
            )

        except AOTYRateLimit:
            # Keep already-found search results rather than failing the whole
            # autocomplete just because AKA enrichment got rate-limited.
            pass

        except Exception:
            pass

        score, matched_aka = _artist_search_score(
            query,
            name,
            akas,
        )

        ranked.append({
            "name": name,
            "url": artist_url,
            "value": (
                "aoty_artist:"
                + artist_url
                .split(
                    "/artist/",
                    1,
                )[1]
                .strip("/")
            ),
            "score": score,
            "akas": akas,
            "matched_aka": matched_aka,
        })

    ranked.sort(
        key=lambda item: item[
            "score"
        ],
        reverse=True,
    )

    results = ranked[
        :max(
            1,
            int(limit),
        )
    ]

    _ARTIST_SEARCH_CACHE[
        cache_key
    ] = (
        now,
        results,
    )

    return results


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

    matched_aka = (
        best.get(
            "matched_aka"
        )
        or ""
    )

    if (
        best["score"] < 0.28
        and normalize_match_text(query)
        not in normalize_match_text(
            best["name"]
        )
        and normalize_match_text(query)
        not in normalize_match_text(
            matched_aka
        )
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


def _artist_details_row(
    soup: BeautifulSoup,
    label_name: str,
):
    """Find one AOTY artist Details row by its real label.

    Artist pages use rows such as:
        HeeJin, HaSeul, ... / Members
        Tokyo Jihen, ...   / Member Of
        Artemis, ...       / Also Known As
        K-Pop, ...         / Genre

    The exact relation label from AOTY is preserved later, so groups display
    "Members" and soloists display "Member Of".
    """
    wanted = label_name.strip().casefold()
    known_labels = {
        "members",
        "member of",
        "also known as",
        "genre",
        "website",
        "related artists",
    }

    candidates = []

    for string in soup.find_all(string=True):
        normalized = " ".join(
            str(string).split()
        ).casefold()

        cleaned = normalized.lstrip("/ ").strip()

        if cleaned == wanted:
            # Prefer the explicit "/ Label" marker if present.
            if normalized.startswith("/"):
                candidates.insert(0, string)
            else:
                candidates.append(string)

    for label in candidates:
        node = label.parent
        best = node

        for _ in range(7):
            if node is None:
                break

            labels_here = set()

            for part in node.stripped_strings:
                normalized = " ".join(
                    str(part).split()
                ).casefold().lstrip("/ ").strip()

                if normalized in known_labels:
                    labels_here.add(normalized)

            if len(labels_here) > 1:
                break

            if wanted in labels_here:
                best = node

            node = node.parent

        if best is not None:
            return best

    return None


def _artist_row_text(
    row,
    label_name: str,
) -> str | None:
    if not row:
        return None

    value = " ".join(
        row.get_text(
            " ",
            strip=True,
        ).split()
    )

    # Remove AOTY's trailing "/ Members", "/ Member Of", etc.
    value = re.sub(
        rf"\s*/\s*{re.escape(label_name)}\s*$",
        "",
        value,
        flags=re.IGNORECASE,
    )

    # AOTY sometimes puts a "+N more..." control inside the AKA row.
    value = re.sub(
        r"(?:,\s*)?\+\d+\s+more\.{0,3}",
        "",
        value,
        flags=re.IGNORECASE,
    )

    value = value.strip(
        " /,"
    )

    return value or None


def _artist_link_values(
    row,
    href_fragment: str,
) -> list[dict]:
    """Return unique linked values from an artist Details row."""
    if not row:
        return []

    results = []
    seen = set()

    for link in row.select(
        f'a[href*="{href_fragment}"]'
    ):
        name = " ".join(
            link.get_text(
                " ",
                strip=True,
            ).split()
        )

        href = link.get(
            "href",
            "",
        )

        if not name or not href:
            continue

        url = urljoin(
            BASE_URL,
            href,
        )

        key = (
            name.casefold(),
            url,
        )

        if key in seen:
            continue

        seen.add(
            key
        )

        results.append({
            "name": name,
            "url": url,
        })

    return results


def _artist_plain_values(
    row,
    label_name: str,
) -> list[str]:
    """Split a plain AOTY Details row into comma-separated values."""
    text = _artist_row_text(
        row,
        label_name,
    )

    if not text:
        return []

    values = []
    seen = set()

    for part in text.split(","):
        value = " ".join(
            part.split()
        ).strip()

        if not value:
            continue

        # Do not leak the expand-control text into AKAs.
        if re.fullmatch(
            r"\+\d+\s+more\.{0,3}",
            value,
            flags=re.IGNORECASE,
        ):
            continue

        key = value.casefold()

        if key in seen:
            continue

        seen.add(
            key
        )

        values.append(
            value
        )

    return values


def _extract_artist_user_score(
    soup: BeautifulSoup,
) -> str | None:
    """Read the headline User Score from an AOTY artist page."""
    for string in soup.find_all(string=True):
        if " ".join(
            str(string).split()
        ).casefold() != "user score":
            continue

        checked = 0

        for next_string in string.parent.find_all_next(
            string=True
        ):
            value = " ".join(
                str(next_string).split()
            )

            if not value:
                continue

            match = re.fullmatch(
                r"(100|\d{1,2}|NR)",
                value,
                flags=re.IGNORECASE,
            )

            if match:
                return match.group(1).upper()

            checked += 1

            if checked >= 15:
                break

    return None


def _extract_artist_ratings_count(
    soup: BeautifulSoup,
) -> str | None:
    """Read artist-level ``Based on N ratings`` next to User Score."""
    pattern = re.compile(
        r"\bBased\s+on\s+"
        r"([\d][\d,.]*(?:\s*[KM])?)"
        r"\s+ratings\b",
        flags=re.IGNORECASE,
    )

    for string in soup.find_all(string=True):
        if " ".join(
            str(string).split()
        ).casefold() != "user score":
            continue

        parts = []
        checked = 0

        for next_string in string.parent.find_all_next(
            string=True
        ):
            value = " ".join(
                str(next_string)
                .replace("\xa0", " ")
                .split()
            )

            if not value:
                continue

            parts.append(
                value
            )

            neighborhood = " ".join(
                parts[-10:]
            )

            match = pattern.search(
                neighborhood
            )

            if match:
                return re.sub(
                    r"\s+",
                    "",
                    match.group(1),
                )

            checked += 1

            if checked >= 25:
                break

    page_text = " ".join(
        soup.get_text(
            " ",
            strip=True,
        )
        .replace("\xa0", " ")
        .split()
    )

    match = re.search(
        r"\bUser\s*Score\b"
        r".{0,100}?"
        r"\bBased\s+on\s+"
        r"([\d][\d,.]*(?:\s*[KM])?)"
        r"\s+ratings\b",
        page_text,
        flags=re.IGNORECASE,
    )

    if match:
        return re.sub(
            r"\s+",
            "",
            match.group(1),
        )

    return None



def _extract_artist_followers(
    soup: BeautifulSoup,
) -> str | None:
    """Read artist followers from the AOTY artist-page header.

    Current AOTY layout:
        Follow
        184 Followers

    The parser is anchored near the Follow control first so it does not grab
    unrelated follower text from another part of the page.
    """
    followers_pattern = re.compile(
        r"^\s*([\d][\d,.]*(?:\s*[KM])?)\s+Followers\s*$",
        flags=re.IGNORECASE,
    )

    # 1. Strongest path: find the Follow control and inspect nearby text.
    for string in soup.find_all(string=True):
        if " ".join(str(string).split()).casefold() != "follow":
            continue

        checked = 0

        for next_string in string.parent.find_all_next(
            string=True
        ):
            value = " ".join(
                str(next_string)
                .replace("\xa0", " ")
                .split()
            )

            if not value:
                continue

            match = followers_pattern.fullmatch(
                value
            )

            if match:
                return re.sub(
                    r"\s+",
                    "",
                    match.group(1),
                )

            checked += 1

            if checked >= 12:
                break

    # 2. Strict whole-page fallback.
    page_text = " ".join(
        soup.get_text(
            " ",
            strip=True,
        )
        .replace("\xa0", " ")
        .split()
    )

    match = re.search(
        r"\bFollow\b.{0,80}?"
        r"([\d][\d,.]*(?:\s*[KM])?)"
        r"\s+Followers\b",
        page_text,
        flags=re.IGNORECASE,
    )

    if match:
        return re.sub(
            r"\s+",
            "",
            match.group(1),
        )

    return None


def _extract_artist_metadata(
    soup: BeautifulSoup,
) -> dict:
    """Parse the public Details block exactly along AOTY's labels."""
    members_row = _artist_details_row(
        soup,
        "Members",
    )

    member_of_row = _artist_details_row(
        soup,
        "Member Of",
    )

    aka_row = _artist_details_row(
        soup,
        "Also Known As",
    )

    genre_row = _artist_details_row(
        soup,
        "Genre",
    )

    members = _artist_link_values(
        members_row,
        "/artist/",
    )

    member_of = _artist_link_values(
        member_of_row,
        "/artist/",
    )

    # Genre links are the cleanest representation and avoid labels/UI text.
    genre_links = _artist_link_values(
        genre_row,
        "/genre/",
    )

    genres = [
        item["name"]
        for item in genre_links
    ]

    # Fallback in case AOTY changes genre links to plain text.
    if not genres:
        genres = _artist_plain_values(
            genre_row,
            "Genre",
        )

    akas = _artist_plain_values(
        aka_row,
        "Also Known As",
    )

    if members:
        relation_label = "Members"
        relation = members
    elif member_of:
        relation_label = "Member Of"
        relation = member_of
    else:
        relation_label = None
        relation = []

    return {
        "artist_user_score": (
            _extract_artist_user_score(
                soup
            )
            or "NR"
        ),
        "artist_ratings_count": (
            _extract_artist_ratings_count(
                soup
            )
            or "0"
        ),
        "artist_followers": (
            _extract_artist_followers(
                soup
            )
            or "0"
        ),

        "genres": genres,
        "genres_text": (
            ", ".join(genres)
            if genres
            else None
        ),

        # Both raw lists remain available for future embeds/code.
        "members": members,
        "member_of": member_of,

        # Actual AOTY relation displayed for this artist.
        "relation_label": relation_label,
        "relation": relation,

        "akas": akas,
        "akas_text": (
            ", ".join(akas)
            if akas
            else None
        ),
    }


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

    artist_metadata = _extract_artist_metadata(
        soup
    )

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

        date_match = re.search(
            r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
            r"\s+\d{1,2},\s+(?:19|20)\d{2}\b",
            container_text,
            flags=re.IGNORECASE,
        )
        release_date = date_match.group(0) if date_match else year
        release_format = _extract_release_format(container_text)

        user_score = None
        score_patterns = (
            r"\buser\s*score\s*(100|\d{1,2})(?!\d)",
            r"\b(100|\d{1,2})\s*user\s*score\b",
        )
        for pattern in score_patterns:
            score_match = re.search(
                pattern,
                container_text,
                flags=re.IGNORECASE,
            )
            if score_match:
                user_score = score_match.group(1)
                break

        ratings_count = None
        count_patterns = (
            r"user\s*score\s*\(([\d,.]+(?:\s*[KM])?)\)",
            r"based\s+on\s+([\d,.]+(?:\s*[KM])?)\s+ratings",
        )
        for pattern in count_patterns:
            count_match = re.search(
                pattern,
                container_text,
                flags=re.IGNORECASE,
            )
            if count_match:
                ratings_count = count_match.group(1).replace(" ", "")
                break

        album_url = urljoin(BASE_URL, href)

        releases.append({
            "album_id": album_id,
            "title": title,
            "album": title,
            "artist": artist_name,
            "url": album_url,
            "year": year,
            "release_date": release_date,
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

        # Artist-level headline/details variables.
        **artist_metadata,

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


def _extract_ratings_count(
    soup: BeautifulSoup,
) -> str | None:
    """Extract album-level AOTY rating count.

    On the public album page the reliable visible layout is:

        User Score
        90
        Based on 16,391 ratings

    The first "Based on N ratings" BEFORE Details is the album count.  This is
    intentionally simpler and more robust than trying to depend on one exact
    link/class name, which AOTY changes fairly often.
    """

    def normalize_count(value):
        if value is None:
            return None

        value = str(value).replace(
            "\xa0",
            " ",
        )

        value = re.sub(
            r"\s+",
            "",
            value,
        )

        return value or None

    page_text = " ".join(
        soup.get_text(
            " ",
            strip=True,
        )
        .replace("\xa0", " ")
        .split()
    )

    # --------------------------------------------------------
    # 1. The top summary block only.
    # --------------------------------------------------------
    top_text = re.split(
        r"\bDetails\b",
        page_text,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]

    match = re.search(
        r"\bBased\s+on\s+"
        r"([\d][\d,.]*(?:\s*[KM])?)"
        r"\s+ratings\b",
        top_text,
        flags=re.IGNORECASE,
    )

    if match:
        return normalize_count(
            match.group(1)
        )

    # --------------------------------------------------------
    # 2. Explicit User Score -> Based on N ratings window.
    # --------------------------------------------------------
    match = re.search(
        r"\bUser\s*Score\b"
        r".{0,260}?"
        r"\bBased\s+on\s+"
        r"([\d][\d,.]*(?:\s*[KM])?)"
        r"\s+ratings\b",
        page_text,
        flags=re.IGNORECASE,
    )

    if match:
        return normalize_count(
            match.group(1)
        )

    # --------------------------------------------------------
    # 3. AOTY User Reviews layout:
    #       User Score (16,298)
    # --------------------------------------------------------
    match = re.search(
        r"\bUser\s*Score\s*\(\s*"
        r"([\d][\d,.]*(?:\s*[KM])?)"
        r"\s*\)",
        page_text,
        flags=re.IGNORECASE,
    )

    if match:
        return normalize_count(
            match.group(1)
        )

    # --------------------------------------------------------
    # 4. DOM fallback: any compact node containing exactly
    #    "Based on N ratings".
    # --------------------------------------------------------
    pattern = re.compile(
        r"\bBased\s+on\s+"
        r"([\d][\d,.]*(?:\s*[KM])?)"
        r"\s+ratings\b",
        flags=re.IGNORECASE,
    )

    for element in soup.find_all(
        ["div", "span", "a", "p"],
    ):
        value = " ".join(
            element.get_text(
                " ",
                strip=True,
            )
            .replace("\xa0", " ")
            .split()
        )

        if not value or len(value) > 120:
            continue

        match = pattern.search(
            value
        )

        if match:
            return normalize_count(
                match.group(1)
            )

    return None


def _album_user_reviews_url(
    album_url: str,
) -> str:
    """Build AOTY's /user-reviews/ route from a public album URL."""
    value = str(
        album_url or ""
    ).split(
        "?",
        1,
    )[0].rstrip("/")

    value = re.sub(
        r"\.php$",
        "",
        value,
        flags=re.IGNORECASE,
    )

    return (
        value
        + "/user-reviews/"
    )


def _fetch_album_ratings_count_fallback(
    album_url: str,
) -> str | None:
    """Dedicated fallback used only when the overview count was not parsed."""
    if not album_url:
        return None

    reviews_url = _album_user_reviews_url(
        album_url
    )

    soup = BeautifulSoup(
        fetch_page(
            reviews_url
        ),
        "html.parser",
    )

    return _extract_ratings_count(
        soup
    )


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


def _extract_album_artist_info(
    soup: BeautifulSoup,
) -> tuple[str | None, str | None]:
    """Return album artist name + exact AOTY artist URL."""
    heading = soup.find(
        "h1"
    )

    candidates = []

    if heading is not None:
        # Artist is normally immediately before the album H1.
        candidates.extend(
            heading.find_all_previous(
                "a",
                href=True,
                limit=12,
            )
        )

    candidates.extend(
        soup.select(
            'a[href*="/artist/"]'
        )
    )

    seen = set()

    for link in candidates:
        href = str(
            link.get(
                "href",
                "",
            )
        )

        if "/artist/" not in href:
            continue

        name = " ".join(
            link.get_text(
                " ",
                strip=True,
            ).split()
        )

        if not name:
            continue

        url = urljoin(
            BASE_URL,
            href,
        )

        key = (
            name.casefold(),
            url,
        )

        if key in seen:
            continue

        seen.add(
            key
        )

        return (
            name,
            url,
        )

    return (
        None,
        None,
    )


def get_album_details(album_url: str) -> dict:
    soup = BeautifulSoup(fetch_page(album_url), "html.parser")
    page_text = " ".join(soup.get_text(" ", strip=True).split())

    album_heading = soup.find("h1")
    album_title = (
        " ".join(
            album_heading.get_text(
                " ",
                strip=True,
            ).split()
        )
        if album_heading
        else None
    )

    artist_name, artist_url = _extract_album_artist_info(
        soup
    )

    user_score = _extract_aoty_user_score(soup)
    ratings_count = _extract_ratings_count(soup)

    if ratings_count is None:
        try:
            ratings_count = _fetch_album_ratings_count_fallback(
                album_url
            )
        except AOTYRateLimit:
            raise
        except Exception:
            ratings_count = None

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
        "album": album_title,
        "artist": artist_name,
        "artist_url": artist_url,
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


def _extract_user_release_url_from_element(
    element,
    album_id: str,
) -> str | None:
    """Extract an AOTY user-release URL from href/data-*/onclick attributes.

    Rating-card action icons are not always normal links.  Depending on the
    AOTY layout, the exact /user/<name>/album/<id>-<slug>/ target can be kept
    in data-url, data-href or onclick.  Keeping that URL avoids an expensive
    ratings-list fallback when Recenzja / Track ratings is clicked.
    """
    if not hasattr(element, "get"):
        return None

    album_id = str(album_id)
    values = []

    attrs = getattr(element, "attrs", {}) or {}

    for _, raw_value in attrs.items():
        if isinstance(raw_value, (list, tuple)):
            raw_value = " ".join(
                str(part)
                for part in raw_value
            )

        value = unescape(
            str(raw_value or "")
        ).strip()

        if value:
            values.append(value)

    href = unescape(
        str(element.get("href", "") or "")
    ).strip()

    if href:
        values.insert(0, href)

    for value in values:
        # Normal absolute/relative path.
        match = re.search(
            rf"(/user/[^/'\"<>\s]+/album/"
            rf"{re.escape(album_id)}[^'\"<>\s]*)",
            value,
            flags=re.IGNORECASE,
        )

        if not match:
            # JS-escaped path: \/user\/name\/album\/123-title\/
            escaped = value.replace("\\/", "/")
            match = re.search(
                rf"(/user/[^/'\"<>\s]+/album/"
                rf"{re.escape(album_id)}[^'\"<>\s]*)",
                escaped,
                flags=re.IGNORECASE,
            )

        if not match:
            continue

        path = match.group(1).rstrip(
            "'\");,]}"
        )

        return urljoin(
            BASE_URL,
            path,
        )

    return None


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

        # Keep the exact user-release URL even when AOTY stores it in data-*
        # or onclick instead of href.
        element_user_url = _extract_user_release_url_from_element(
            element,
            album_id,
        )

        if element_user_url:
            review_url = element_user_url

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
    artist_url = None

    artist_element = block.select_one(
        ".artistTitle"
    )

    artist_link = None

    if artist_element:
        artist = clean_text(
            artist_element
        )

        if (
            artist_element.name == "a"
            and artist_element.get("href")
        ):
            artist_link = artist_element
        else:
            artist_link = artist_element.select_one(
                'a[href*="/artist/"]'
            )

    if artist_link is None:
        artist_link = block.select_one(
            'a[href*="/artist/"]'
        )

    if not artist and artist_link:
        artist = clean_text(
            artist_link
        )

    if artist_link and artist_link.get(
        "href"
    ):
        artist_url = urljoin(
            BASE_URL,
            artist_link.get(
                "href"
            ),
        )

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
        "artist_url": artist_url,
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
    limit: int | None = 60,
    forced_format: str | None = None,
    *,
    max_pages: int = 100,
) -> list[dict]:
    """Read a ratings route page-by-page.

    ``limit=None`` means *no rating-count cap* and is reserved for the durable
    profile archive. ``max_pages`` is only a corruption/infinite-pagination
    guard: hitting it while AOTY still returns new rows raises instead of
    pretending the archive is complete.
    """
    unlimited = limit is None

    if not unlimited:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 60

        if limit <= 0:
            return []

    max_pages = max(1, int(max_pages))
    all_ratings = []
    seen = set()
    page = 1

    while unlimited or len(all_ratings) < int(limit):
        if page > max_pages:
            raise AOTYArchiveIncomplete(
                f"Przerwano paginację {username}/{slug or 'all'} po "
                f"{max_pages} stronach; format nie zostanie oznaczony jako pełny."
            )

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

            if not unlimited and len(all_ratings) >= int(limit):
                break

        # Repeated page/canonical redirect: stop instead of looping forever.
        if new_on_page == 0:
            break

        page += 1

    if unlimited:
        return all_ratings

    return all_ratings[: int(limit)]


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


def get_all_ratings_for_format(username: str, format_key: str) -> list[dict]:
    """Fetch an entire rating format for the persistent configured-user archive.

    There is intentionally no rating-count limit. A high page guard protects
    Kotone if AOTY starts returning the same/invalid pagination forever.
    """
    info = RATING_FORMATS.get(str(format_key))

    if not info:
        return []

    return _get_ratings_from_route(
        username=username,
        slug=info["slug"],
        limit=None,
        forced_format=info["label"],
        max_pages=AOTY_ARCHIVE_MAX_PAGES,
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

def _aoty_user_album_slug(title: str | None) -> str:
    """Build the slug used by AOTY user-release URLs from the release title.

    Public release URL:
        /album/1225702-kiiikiii-uncut-gem.php

    User release URL:
        /user/<name>/album/1225702-uncut-gem/

    The artist portion is intentionally NOT included.
    """
    if not title:
        return ""

    value = unicodedata.normalize(
        "NFKD",
        str(title),
    )

    value = "".join(
        char
        for char in value
        if not unicodedata.combining(char)
    )

    value = value.casefold()
    value = value.replace("&", " and ")
    value = re.sub(
        r"[^a-z0-9]+",
        "-",
        value,
    ).strip("-")

    return value


def _fetch_user_release_page(
    username: str,
    album_id: str,
    album_url: str | None,
    user_release_url: str | None = None,
    album_title: str | None = None,
):
    """Fetch the user's page for one release.

    /last must not depend on finding the exact href in a rating card first.
    We therefore try:
      1. exact captured user-release URL;
      2. predictable /user/<name>/album/<id>-<album-title>/ URL;
      3. ID-only route;
      4. old public-URL-derived fallback.

    AOTY can canonicalize/redirect URLs.  A successful page is accepted by
    CONTENT as well as URL, rather than being rejected only because the final
    URL differs slightly.
    """
    username = str(
        username
    ).strip()

    album_id = str(
        album_id
    ).strip()

    candidates = []

    def add_candidate(url):
        if not url:
            return

        absolute = urljoin(
            BASE_URL,
            str(url).strip(),
        )

        if absolute not in candidates:
            candidates.append(
                absolute
            )

    add_candidate(
        user_release_url
    )

    title_slug = _aoty_user_album_slug(
        album_title
    )

    if title_slug:
        add_candidate(
            f"{BASE_URL}/user/{username}/album/"
            f"{album_id}-{title_slug}/"
        )

    add_candidate(
        f"{BASE_URL}/user/{username}/album/{album_id}/"
    )

    if album_url and "/album/" in str(album_url):
        release_path = (
            str(album_url)
            .split("/album/", 1)[1]
            .strip("/")
        )

        if release_path:
            release_path = re.sub(
                r"\.php$",
                "",
                release_path,
                flags=re.IGNORECASE,
            )

            add_candidate(
                f"{BASE_URL}/user/{username}/album/"
                f"{release_path}/"
            )

    last_url = None

    for candidate in candidates:
        last_url = candidate

        try:
            result = HTTP.get(
                candidate,
                allow_stale=True,
            )
        except ExternalRateLimit as exc:
            raise AOTYRateLimit(str(exc)) from exc
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                continue
            raise
        except ExternalUnavailable as exc:
            raise requests.ConnectionError(str(exc)) from exc

        soup = BeautifulSoup(
            result.text,
            "html.parser",
        )

        final_url = str(
            result.url
        )

        final_lower = final_url.casefold()

        # URL validation when AOTY keeps the canonical user-release route.
        url_looks_right = (
            f"/user/{username.casefold()}/album/"
            in final_lower
            and album_id in final_lower
        )

        # Content validation protects against harmless canonicalization while
        # still rejecting AOTY's homepage/other unrelated redirects.
        page_text = " ".join(
            soup.get_text(
                " ",
                strip=True,
            ).split()
        )

        page_lower = page_text.casefold()

        title_looks_right = True

        if album_title:
            normalized_title = normalize_match_text(
                album_title
            )

            normalized_page = normalize_match_text(
                page_text[:2500]
            )

            if normalized_title:
                title_looks_right = (
                    normalized_title
                    in normalized_page
                )

        has_user_release_content = any(
            token in page_lower
            for token in (
                "track ratings",
                "play this on",
            )
        )

        # A review page without Track Ratings is still valid when the album
        # title is present and the URL points to this user.
        content_looks_right = (
            title_looks_right
            and (
                has_user_release_content
                or f"/user/{username.casefold()}/album/"
                in final_lower
            )
        )

        if not (
            url_looks_right
            or content_looks_right
        ):
            continue

        return (
            soup,
            final_url,
        )

    return (
        None,
        last_url,
    )


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
    """Extract every track score entered by the user.

    AOTY currently has more than one Track Ratings layout:

        5 | Take Me Thru Dere / 58

    and a compact/card layout where the number can be absent visually:

        DEMONSTRATION        88

    A missing track number is therefore valid.  Numberless rows are matched
    to the public tracklist later by normalized track title.
    """
    marker = None

    for string in soup.find_all(string=True):
        if " ".join(str(string).split()).casefold() == "track ratings":
            marker = string.parent
            break

    if marker is None:
        return []

    results: list[dict] = []
    seen_numbers: set[int] = set()
    seen_titles: set[str] = set()

    def add_result(
        number,
        title,
        score,
    ):
        title = " ".join(
            str(title or "").split()
        ).strip(" -–—/|")

        score = str(
            score or ""
        ).strip()

        if not title:
            return

        if not re.fullmatch(
            r"(100|\d{1,2})",
            score,
        ):
            return

        # Section/UI labels are not track titles.
        if title.casefold() in {
            "track ratings",
            "play this on",
            "amazon",
            "apple music",
            "spotify",
            "bandcamp",
            "vinyl",
            "comments",
        }:
            return

        if re.fullmatch(
            r"\d{1,2}:\d{2}",
            title,
        ):
            return

        title_key = _track_title_key(
            title
        )

        if not title_key:
            return

        parsed_number = None

        if number is not None:
            try:
                parsed_number = int(number)
            except (TypeError, ValueError):
                parsed_number = None

        # Prefer title deduplication because compact AOTY rows may have no
        # number, while nested parent elements can repeat the same text.
        if title_key in seen_titles:
            return

        if (
            parsed_number is not None
            and parsed_number in seen_numbers
        ):
            # Same numbered track repeated through nested containers.
            return

        seen_titles.add(
            title_key
        )

        if parsed_number is not None:
            seen_numbers.add(
                parsed_number
            )

        results.append({
            "number": parsed_number,
            "title": title,
            "score": score,
        })

    def section_finished(text: str) -> bool:
        lower = " ".join(
            str(text or "").split()
        ).casefold()

        return lower in {
            "play this on",
            "amazon",
            "apple music",
            "spotify",
            "bandcamp",
            "vinyl",
            "comments",
        }

    # ------------------------------------------------------------------
    # 1. Small row-like containers.
    # ------------------------------------------------------------------
    for element in marker.find_all_next(
        ["tr", "li", "div", "p"],
        limit=500,
    ):
        row_text = " ".join(
            element.get_text(
                " ",
                strip=True,
            ).split()
        )

        if not row_text:
            continue

        if section_finished(
            row_text
        ):
            break

        # Ignore very large parent containers; their children will be checked
        # individually and are much safer to parse.
        if len(row_text) > 300:
            continue

        # Numbered AOTY layout:
        #   5 | Take Me Thru Dere / 58
        match = re.fullmatch(
            r"\s*(\d{1,3})\s*"
            r"(?:[.|:)\-–—|]\s*)?"
            r"(.+?)\s*"
            r"(?:/|—|–|\|)\s*"
            r"(100|\d{1,2})\s*",
            row_text,
        )

        if match:
            add_result(
                match.group(1),
                match.group(2),
                match.group(3),
            )
            continue

        # Numbered but separators flattened:
        #   5 Take Me Thru Dere 58
        match = re.fullmatch(
            r"\s*(\d{1,3})\s+"
            r"(.+?)\s+"
            r"(100|\d{1,2})\s*",
            row_text,
        )

        if match:
            add_result(
                match.group(1),
                match.group(2),
                match.group(3),
            )
            continue

        # Compact/card layout from the supplied screenshot:
        #   DEMONSTRATION 88
        #
        # Require at least one non-digit character in the title so a random
        # "1 88" UI fragment cannot become a fake track.
        match = re.fullmatch(
            r"\s*(.+?\D.*?)\s+"
            r"(100|\d{1,2})\s*",
            row_text,
        )

        if match:
            candidate_title = match.group(1).strip()

            # Do not reinterpret the numbered form as a numberless title.
            if not re.match(
                r"^\d{1,3}\s+[|:.)\-–—]",
                candidate_title,
            ):
                add_result(
                    None,
                    candidate_title,
                    match.group(2),
                )

    # ------------------------------------------------------------------
    # 2. Token fallback.
    #
    # Covers rows split into spans:
    # ["5", "|", "Title", "/", "58"]
    # and the compact form:
    # ["DEMONSTRATION", "88"].
    # ------------------------------------------------------------------
    tokens = []

    for string in marker.find_all_next(
        string=True
    ):
        token = " ".join(
            str(string).split()
        )

        if not token:
            continue

        if section_finished(
            token
        ):
            break

        tokens.append(
            token
        )

        if len(tokens) >= 1400:
            break

    i = 0

    while i < len(tokens):
        token = tokens[i]

        # -----------------------------
        # Numbered token layout
        # -----------------------------
        number_match = re.fullmatch(
            r"(\d{1,3})",
            token,
        )

        if number_match:
            slash_index = None

            for j in range(
                i + 1,
                min(len(tokens), i + 18),
            ):
                if tokens[j] in {
                    "/",
                    "—",
                    "–",
                }:
                    slash_index = j
                    break

            if slash_index is not None:
                score_index = None

                for j in range(
                    slash_index + 1,
                    min(
                        len(tokens),
                        slash_index + 5,
                    ),
                ):
                    if re.fullmatch(
                        r"(100|\d{1,2})",
                        tokens[j],
                    ):
                        score_index = j
                        break

                if score_index is not None:
                    title_parts = [
                        part
                        for part in tokens[
                            i + 1:slash_index
                        ]
                        if part not in {
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
                            number_match.group(1),
                            title,
                            tokens[score_index],
                        )

                    i = score_index + 1
                    continue

        # -----------------------------
        # Numberless compact layout
        # -----------------------------
        if (
            i + 1 < len(tokens)
            and not re.fullmatch(
                r"(100|\d{1,2})",
                token,
            )
            and re.search(
                r"[^\W\d_]",
                token,
                flags=re.UNICODE,
            )
            and re.fullmatch(
                r"(100|\d{1,2})",
                tokens[i + 1],
            )
        ):
            add_result(
                None,
                token,
                tokens[i + 1],
            )

            i += 2
            continue

        i += 1

    results.sort(
        key=lambda item: (
            (
                int(item["number"])
                if item.get("number") is not None
                else 9999
            ),
            _track_title_key(
                item.get("title")
            ),
        )
    )

    return results


def _merge_partial_track_ratings(
    album_tracklist: list[dict],
    user_track_ratings: list[dict],
) -> list[dict]:
    """Overlay user scores on the complete release tracklist safely.

    AOTY sometimes exposes nested DOM containers whose flattened text looks
    like another track row. Example of the bad parse seen in Discord:

        100 | 2 Pitch the Baby / 100

    even though the real row is simply track 2, "Pitch the Baby", score 100.

    This function canonicalizes every parsed rating against the PUBLIC album
    tracklist before rendering. One public track can therefore appear only
    once in the final result.

    Matching priority:
      1. exact normalized track title;
      2. a title accidentally prefixed with its track number
         ("2 Pitch the Baby" -> track 2 "Pitch the Baby");
      3. a valid parsed track number.

    Unrated public tracks remain score=None -> NR.
    Truly unmatched hidden/bonus tracks can still be appended.
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

    # ------------------------------------------------------------------
    # Canonical public-track indexes.
    # ------------------------------------------------------------------
    public_by_number: dict[int, dict] = {}
    public_by_title: dict[str, dict] = {}

    canonical_tracks = []

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

        canonical = dict(track)
        canonical["number"] = number
        canonical["title"] = title

        canonical_tracks.append(
            canonical
        )

        public_by_number[
            number
        ] = canonical

        title_key = _track_title_key(
            title
        )

        if title_key:
            public_by_title[
                title_key
            ] = canonical

    # ------------------------------------------------------------------
    # Resolve every parsed user rating to at most ONE public track.
    # ------------------------------------------------------------------
    resolved_by_number: dict[int, dict] = {}
    unresolved: list[dict] = []

    for rating in user_track_ratings:
        raw_title = " ".join(
            str(
                rating.get("title")
                or ""
            ).split()
        ).strip()

        raw_title_key = _track_title_key(
            raw_title
        )

        parsed_number = rating.get(
            "number"
        )

        try:
            parsed_number = (
                int(parsed_number)
                if parsed_number is not None
                else None
            )
        except (TypeError, ValueError):
            parsed_number = None

        target = None
        confidence = 0

        # --------------------------------------------------------------
        # 1. Exact title.
        # --------------------------------------------------------------
        if raw_title_key:
            target = public_by_title.get(
                raw_title_key
            )

            if target is not None:
                confidence = 30

                if (
                    parsed_number is not None
                    and parsed_number == target["number"]
                ):
                    confidence = 40

        # --------------------------------------------------------------
        # 2. Nested-container artefact:
        #
        #    parsed number: 100
        #    parsed title : "2 Pitch the Baby"
        #
        # Strip the leading number ONLY when the remainder exactly matches
        # the corresponding public track. This avoids breaking legitimate
        # titles that genuinely start with a number.
        # --------------------------------------------------------------
        if target is None and raw_title:
            prefix_match = re.match(
                r"^\s*(\d{1,3})"
                r"(?:[.|:)\-–—|]|\s)+"
                r"(.+?)\s*$",
                raw_title,
            )

            if prefix_match:
                prefix_number = int(
                    prefix_match.group(1)
                )

                stripped_title = (
                    prefix_match.group(2)
                    .strip()
                )

                public_track = public_by_number.get(
                    prefix_number
                )

                if (
                    public_track is not None
                    and _track_title_key(
                        stripped_title
                    )
                    == _track_title_key(
                        public_track.get("title")
                    )
                ):
                    target = public_track
                    confidence = 35

        # --------------------------------------------------------------
        # 3. Parsed number fallback.
        # --------------------------------------------------------------
        if (
            target is None
            and parsed_number is not None
        ):
            target = public_by_number.get(
                parsed_number
            )

            if target is not None:
                confidence = 20

        if target is None:
            unresolved.append(
                rating
            )
            continue

        target_number = target[
            "number"
        ]

        candidate = {
            "number": target_number,
            "title": target.get(
                "title"
            ),
            "score": rating.get(
                "score"
            ),
            "_confidence": confidence,
        }

        existing = resolved_by_number.get(
            target_number
        )

        if (
            existing is None
            or candidate["_confidence"]
            > existing.get(
                "_confidence",
                0,
            )
        ):
            resolved_by_number[
                target_number
            ] = candidate

    # ------------------------------------------------------------------
    # Render exactly one row for each public track.
    # ------------------------------------------------------------------
    merged = []

    for track in canonical_tracks:
        number = track[
            "number"
        ]

        rating = resolved_by_number.get(
            number
        )

        merged.append({
            "number": number,
            "title": track.get(
                "title"
            ),
            "score": (
                rating.get("score")
                if rating
                else None
            ),
            "duration": track.get(
                "duration"
            ),
            "disc": track.get(
                "disc"
            ),
            "url": track.get(
                "url"
            ),
        })

    # ------------------------------------------------------------------
    # Preserve only genuinely unmatched hidden/bonus tracks.
    #
    # Before appending, one final guard removes malformed duplicates whose
    # title becomes a public title after stripping a leading track number.
    # ------------------------------------------------------------------
    seen_extra_keys = set()

    for rating in unresolved:
        title = " ".join(
            str(
                rating.get("title")
                or ""
            ).split()
        ).strip()

        if not title:
            continue

        title_key = _track_title_key(
            title
        )

        if title_key in public_by_title:
            continue

        prefix_match = re.match(
            r"^\s*(\d{1,3})"
            r"(?:[.|:)\-–—|]|\s)+"
            r"(.+?)\s*$",
            title,
        )

        if prefix_match:
            prefix_number = int(
                prefix_match.group(1)
            )

            stripped_title = (
                prefix_match.group(2)
                .strip()
            )

            public_track = public_by_number.get(
                prefix_number
            )

            if (
                public_track is not None
                and _track_title_key(
                    stripped_title
                )
                == _track_title_key(
                    public_track.get("title")
                )
            ):
                # This is the exact duplicate artefact from a parent
                # container. Never append it as a bonus track.
                continue

        extra_key = (
            rating.get("number"),
            title_key,
        )

        if extra_key in seen_extra_keys:
            continue

        seen_extra_keys.add(
            extra_key
        )

        merged.append({
            "number": rating.get(
                "number"
            ),
            "title": title,
            "score": rating.get(
                "score"
            ),
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
    album_title: str | None = None,
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
            "detail_incomplete": False,
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
            album_title=album_title,
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
                    album_title=(
                        item.get("album")
                        or album_title
                    ),
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
            "detail_incomplete": True,
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
        "detail_incomplete": True,
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


def _extract_profile_distribution(soup: BeautifulSoup) -> dict[str, int]:
    """Return the complete Rating Distribution shown on a user profile."""
    marker = _find_exact_text_marker(soup, "rating distribution")
    if marker is None:
        return {}

    labels = (
        "100",
        "90-99",
        "80-89",
        "70-79",
        "60-69",
        "50-59",
        "40-49",
        "30-39",
        "20-29",
        "10-19",
        "0-9",
    )
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
        if len(found) >= len(labels):
            break

    if found:
        return found

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
    for label in labels:
        match = re.search(
            rf"(?<!\d){re.escape(label)}\s+([\d,]+)",
            distribution_text,
        )
        if match:
            found[label] = int(match.group(1).replace(",", ""))

    return found


def _profile_average_from_distribution(distribution: dict[str, int]) -> float | None:
    """Approximate average without reparsing the same profile DOM twice."""
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
    total_count = sum(distribution.values())
    if total_count <= 0:
        return None
    weighted = sum(
        midpoint_map[label] * count
        for label, count in distribution.items()
        if label in midpoint_map
    )
    return weighted / total_count


def _extract_profile_average(soup: BeautifulSoup) -> float | None:
    # Compatibility helper for existing callers/tests.
    return _profile_average_from_distribution(
        _extract_profile_distribution(soup)
    )


def get_profile_summary(username: str) -> dict:
    """Fetch only the profile page, without fetching ratings routes again."""
    username = str(username).strip()
    url = f"{BASE_URL}/user/{username}/"
    soup = BeautifulSoup(fetch_page(url), "html.parser")

    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if " - profile - album of the year" not in title.casefold():
        raise AOTYUserNotFound()

    heading = soup.find("h1")
    display_username = heading.get_text(" ", strip=True) if heading else username
    page_text = " ".join(soup.get_text(" ", strip=True).split())
    distribution = _extract_profile_distribution(soup)
    average_rating = _profile_average_from_distribution(distribution)
    favorite_kind, favorites = _extract_profile_favorites(soup, limit=50)

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
        "rating_distribution": distribution,
        "favorite_kind": favorite_kind,
        "favorites": favorites,
        "favorite_albums": favorite_albums,
        "favorite_artists": favorite_artists,
    }


def get_profile_data(username: str, recent_limit: int = 50) -> dict:
    """Compatibility helper: profile summary + recent ratings."""
    try:
        recent_limit = max(5, min(50, int(recent_limit)))
    except (TypeError, ValueError):
        recent_limit = 50

    profile = get_profile_summary(username)
    profile["recent_ratings"] = get_recent_ratings(username, recent_limit)
    return profile

