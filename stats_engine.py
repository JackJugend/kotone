"""Pure SQLite analytics used by Kotone's statistics commands."""

from __future__ import annotations

import math
import re
import statistics
from collections import Counter, defaultdict
from datetime import UTC, datetime


SCORE_BUCKETS = (
    ("90–100", 90, 100),
    ("80–89", 80, 89),
    ("70–79", 70, 79),
    ("60–69", 60, 69),
    ("0–59", 0, 59),
)


def _score(value) -> float | None:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or not 0 <= number <= 100:
        return None
    return number


def _rating_year(row: dict) -> int | None:
    try:
        timestamp = float(row.get("sort_timestamp") or 0)
    except (TypeError, ValueError):
        timestamp = 0
    if timestamp > 0:
        try:
            return datetime.fromtimestamp(timestamp, UTC).year
        except (OverflowError, OSError, ValueError):
            pass

    text = str(row.get("rating_date") or "")
    years = re.findall(r"(?<!\d)(19\d{2}|20\d{2}|21\d{2})(?!\d)", text)
    return int(years[-1]) if years else None


def _rating_month(row: dict) -> int | None:
    try:
        timestamp = float(row.get("sort_timestamp") or 0)
    except (TypeError, ValueError):
        return None
    if timestamp <= 0:
        return None
    try:
        return datetime.fromtimestamp(timestamp, UTC).month
    except (OverflowError, OSError, ValueError):
        return None


def _release_year(row: dict) -> int | None:
    text = str(row.get("release_year") or row.get("release_date") or "")
    match = re.search(r"(?<!\d)(19\d{2}|20\d{2}|21\d{2})(?!\d)", text)
    return int(match.group(1)) if match else None


def _clean_values(values) -> list[str]:
    return [str(value).strip() for value in values or [] if str(value).strip()]


def _top(counter: Counter, limit: int = 5) -> list[tuple[str, int]]:
    return sorted(counter.items(), key=lambda item: (-item[1], item[0].casefold()))[:limit]


def summarize(username: str, rows: list[dict]) -> dict:
    numeric = [(row, _score(row.get("score"))) for row in rows]
    numeric = [(row, score) for row, score in numeric if score is not None]
    scores = [score for _, score in numeric]

    formats = Counter(
        str(row.get("release_format") or "Nieznany").strip() or "Nieznany"
        for row, _ in numeric
    )
    genres = Counter()
    artists = Counter()
    decades = Counter()
    buckets = Counter({label: 0 for label, _, _ in SCORE_BUCKETS})

    for row, score in numeric:
        artists[str(row.get("artist") or "Nieznany artysta").strip()] += 1
        genres.update(_clean_values(row.get("genres")))
        year = _release_year(row)
        if year is not None:
            decades[f"{year // 10 * 10}s"] += 1
        for label, lower, upper in SCORE_BUCKETS:
            if lower <= score <= upper:
                buckets[label] += 1
                break

    top_ratings = sorted(
        numeric,
        key=lambda item: (
            -item[1],
            str(item[0].get("artist") or "").casefold(),
            str(item[0].get("album") or "").casefold(),
        ),
    )[:5]

    return {
        "username": username,
        "ratings": len(scores),
        "average": statistics.fmean(scores) if scores else None,
        "median": statistics.median(scores) if scores else None,
        "minimum": min(scores) if scores else None,
        "maximum": max(scores) if scores else None,
        "reviews": sum(bool(row.get("has_review")) for row in rows),
        "likes": sum(bool(row.get("liked")) for row in rows),
        "track_albums": sum(bool(row.get("has_track_ratings")) for row in rows),
        "track_scores": sum(int(row.get("track_score_count") or 0) for row in rows),
        "top_formats": _top(formats),
        "top_genres": _top(genres),
        "top_artists": _top(artists),
        "top_decades": _top(decades),
        "score_buckets": [(label, buckets[label]) for label, _, _ in SCORE_BUCKETS],
        "top_ratings": [
            {
                "album_id": row.get("album_id"),
                "artist": row.get("artist") or "Nieznany artysta",
                "album": row.get("album") or "Nieznane wydanie",
                "score": score,
            }
            for row, score in top_ratings
        ],
    }


def compare(user_a: str, rows_a: list[dict], user_b: str, rows_b: list[dict]) -> dict:
    map_a = {
        str(row.get("album_id")): (row, score)
        for row in rows_a
        if (score := _score(row.get("score"))) is not None
    }
    map_b = {
        str(row.get("album_id")): (row, score)
        for row in rows_b
        if (score := _score(row.get("score"))) is not None
    }
    common_ids = set(map_a) & set(map_b)
    common = []
    for album_id in common_ids:
        row_a, score_a = map_a[album_id]
        row_b, score_b = map_b[album_id]
        common.append(
            {
                "album_id": album_id,
                "artist": row_a.get("artist") or row_b.get("artist") or "Nieznany artysta",
                "album": row_a.get("album") or row_b.get("album") or "Nieznane wydanie",
                "score_a": score_a,
                "score_b": score_b,
                "gap": abs(score_a - score_b),
                "mean": (score_a + score_b) / 2,
            }
        )

    gaps = [item["gap"] for item in common]
    shared_genres_a = Counter(
        genre
        for album_id in common_ids
        for genre in _clean_values(map_a[album_id][0].get("genres"))
    )
    shared_genres_b = Counter(
        genre
        for album_id in common_ids
        for genre in _clean_values(map_b[album_id][0].get("genres"))
    )
    shared_genres = shared_genres_a + shared_genres_b

    return {
        "user_a": user_a,
        "user_b": user_b,
        "ratings_a": len(map_a),
        "ratings_b": len(map_b),
        "average_a": statistics.fmean(value[1] for value in map_a.values()) if map_a else None,
        "average_b": statistics.fmean(value[1] for value in map_b.values()) if map_b else None,
        "common_count": len(common),
        "mean_gap": statistics.fmean(gaps) if gaps else None,
        "agreement": max(0.0, 100.0 - statistics.fmean(gaps)) if gaps else None,
        "disagreements": sorted(
            common,
            key=lambda item: (-item["gap"], -item["mean"], item["album"].casefold()),
        )[:5],
        "shared_favorites": sorted(
            common,
            key=lambda item: (-item["mean"], item["gap"], item["album"].casefold()),
        )[:5],
        "shared_genres": _top(shared_genres),
    }


def wrapped(username: str, rows: list[dict], year: int) -> dict:
    selected = [row for row in rows if _rating_year(row) == year]
    result = summarize(username, selected)
    result["year"] = year

    months = Counter()
    for row in selected:
        month = _rating_month(row)
        if month is not None:
            months[month] += 1
    result["months"] = [(month, months[month]) for month in range(1, 13)]

    artist_scores: dict[str, list[float]] = defaultdict(list)
    for row in selected:
        score = _score(row.get("score"))
        if score is not None:
            artist_scores[str(row.get("artist") or "Nieznany artysta")].append(score)
    result["artist_averages"] = sorted(
        (
            (artist, len(scores), statistics.fmean(scores))
            for artist, scores in artist_scores.items()
        ),
        key=lambda item: (-item[1], -item[2], item[0].casefold()),
    )[:5]
    return result
