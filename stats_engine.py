"""Pure SQLite analytics used by Kotone's statistics commands."""

from __future__ import annotations

import math
import re
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from must_hear import must_hear_album
from formats import format_key_from_label
from time_utils import POLISH_TIMEZONE, polish_datetime, polish_now


SCORE_BUCKETS = (
    ("100", 100, 100),
    ("90–99", 90, 99),
    ("80–89", 80, 89),
    ("70–79", 70, 79),
    ("60–69", 60, 69),
    ("50–59", 50, 59),
    ("40–49", 40, 49),
    ("30–39", 30, 39),
    ("20–29", 20, 29),
    ("10–19", 10, 19),
    ("0–9", 0, 9),
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
    rated_on = _activity_date(row)
    if rated_on is not None:
        return rated_on.year

    text = str(row.get("rating_date") or "")
    years = re.findall(r"(?<!\d)(19\d{2}|20\d{2}|21\d{2})(?!\d)", text)
    return int(years[-1]) if years else None


def _rating_month(row: dict) -> int | None:
    rated_on = _activity_date(row)
    return rated_on.month if rated_on is not None else None


def filter_rating_rows(
    rows: list[dict],
    *,
    release_year: int | None = None,
    genre: str | None = None,
    release_format: str | None = None,
    score_min: int | None = None,
    score_max: int | None = None,
    reviewed: bool | None = None,
    liked: bool | None = None,
    has_tracks: bool | None = None,
    artist: str | None = None,
) -> list[dict]:
    """Apply the common graphic-command filters to an analytics snapshot."""

    genre_key = str(genre or "").strip().casefold()
    artist_key = str(artist or "").strip().casefold()
    format_key = format_key_from_label(release_format) if release_format else None
    selected = []
    for row in rows:
        score = _score(row.get("score"))
        if release_year is not None and _release_year(row) != release_year:
            continue
        if genre_key and genre_key not in {
            value.casefold() for value in _clean_values(row.get("genres"))
        }:
            continue
        if format_key and format_key_from_label(row.get("release_format")) != format_key:
            continue
        if score_min is not None and (score is None or score < score_min):
            continue
        if score_max is not None and (score is None or score > score_max):
            continue
        if reviewed is not None and bool(row.get("has_review")) is not reviewed:
            continue
        if liked is not None and bool(row.get("liked")) is not liked:
            continue
        if has_tracks is not None and bool(row.get("has_track_ratings")) is not has_tracks:
            continue
        if artist_key and artist_key not in str(row.get("artist") or "").casefold():
            continue
        selected.append(row)
    return selected


_CHART_MONTHS = (
    "Sty", "Lut", "Mar", "Kwi", "Maj", "Cze",
    "Lip", "Sie", "Wrz", "Paź", "Lis", "Gru",
)
_ENGLISH_MONTHS = {
    name: month
    for month, names in enumerate(
        (
            ("jan", "january"), ("feb", "february"), ("mar", "march"),
            ("apr", "april"), ("may",), ("jun", "june"), ("jul", "july"),
            ("aug", "august"), ("sep", "sept", "september"),
            ("oct", "october"), ("nov", "november"), ("dec", "december"),
        ),
        start=1,
    )
    for name in names
}


def _activity_date(row: dict) -> date | None:
    """Keep calendar dates intact; render timestamp-only ratings in Warsaw."""

    # CSV dates have no time zone. Their synthetic sorting timestamp may land
    # on the following Polish day, so the explicit calendar date takes priority.
    text = " ".join(str(row.get("rating_date") or "").split())
    for pattern in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            pass

    # AOTY calendar dates must have an explicit year. Relative strings and
    # yearless dates cannot safely be reconstructed from an old database row.
    match = re.fullmatch(
        r"([A-Za-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?[,]?\s+(\d{4})",
        text,
        flags=re.IGNORECASE,
    )
    if match:
        month = _ENGLISH_MONTHS.get(match.group(1).casefold())
        if month is not None:
            try:
                return date(int(match.group(3)), month, int(match.group(2)))
            except ValueError:
                pass
    try:
        timestamp = float(row.get("sort_timestamp") or 0)
        if math.isfinite(timestamp) and timestamp > 0:
            return polish_datetime(timestamp).date()
    except (TypeError, OverflowError, OSError, ValueError):
        pass
    return None


def _activity_bucket_start(day: date, chart_type: str) -> date:
    if chart_type == "daily":
        return day
    if chart_type == "weekly":
        return day - timedelta(days=day.weekday())
    if chart_type == "monthly":
        return day.replace(day=1)
    return day.replace(month=1, day=1)


def _activity_bucket_shift(start: date, chart_type: str, offset: int) -> date:
    if chart_type == "daily":
        return start + timedelta(days=offset)
    if chart_type == "weekly":
        return start + timedelta(weeks=offset)
    if chart_type == "monthly":
        year, month = divmod(start.year * 12 + start.month - 1 + offset, 12)
        return date(year, month + 1, 1)
    return date(start.year + offset, 1, 1)


def rating_activity(
    username: str,
    rows: list[dict],
    chart_type: str = "monthly",
    period: int = 12,
    *,
    now: datetime | None = None,
) -> dict:
    """Count active release ratings in Polish calendar periods, including DST.

    The current day/week/month/year is included even when it is incomplete.
    ``rows`` is the analytics read model: one row per active saved release
    rating, so edits, event history and individual track scores add no counts.
    """

    if chart_type not in ("daily", "weekly", "monthly", "yearly"):
        raise ValueError("Nieznany typ wykresu.")
    if isinstance(period, bool) or not isinstance(period, int) or not 1 <= period <= 365:
        raise ValueError("Liczba okresów musi być liczbą całkowitą od 1 do 365.")
    if now is None:
        now = polish_now()
    elif not isinstance(now, datetime):
        raise ValueError("Bieżący czas musi być datą i godziną.")
    if now.tzinfo is None:
        now = now.replace(tzinfo=POLISH_TIMEZONE)
    today = now.astimezone(POLISH_TIMEZONE).date()
    current_start = _activity_bucket_start(today, chart_type)
    buckets = []
    bucket_by_start = {}
    for offset in range(1 - period, 1):
        start = _activity_bucket_shift(current_start, chart_type, offset)
        end = _activity_bucket_shift(start, chart_type, 1)
        if chart_type == "monthly":
            label = f"{_CHART_MONTHS[start.month - 1]} {start.year}"
        elif chart_type == "yearly":
            label = str(start.year)
        else:
            label = start.strftime("%d.%m.%Y")
        bucket = {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "label": label,
            "count": 0,
            "score_counts": {label: 0 for label, _, _ in SCORE_BUCKETS},
        }
        buckets.append(bucket)
        bucket_by_start[start] = bucket

    undated_ratings = 0
    selected_scores = []
    for row in rows:
        score = _score(row.get("score"))
        if row.get("_track_score") or score is None:
            continue
        rated_on = _activity_date(row)
        if rated_on is None:
            undated_ratings += 1
            continue
        # Export timestamps encode date-only rows close to midnight. A time
        # later today is still today's rating; only future dates are excluded.
        if rated_on > today:
            continue
        bucket = bucket_by_start.get(_activity_bucket_start(rated_on, chart_type))
        if bucket is not None:
            bucket["count"] += 1
            selected_scores.append(score)
            # Fractional export scores stay in their decade (89.5 in 80–89).
            score_label = next(
                label for label, lower, upper in SCORE_BUCKETS
                if lower <= score < upper + 1
            )
            bucket["score_counts"][score_label] += 1

    ratings = sum(bucket["count"] for bucket in buckets)
    return {
        "username": username,
        "chart_type": chart_type,
        "period": period,
        "buckets": buckets,
        "ratings": ratings,
        "average": ratings / period,
        "average_score": statistics.fmean(selected_scores) if selected_scores else None,
        "peak": max(bucket["count"] for bucket in buckets),
        "undated_ratings": undated_ratings,
        "range_start": buckets[0]["start"],
        "range_end": today.isoformat(),
        "current_period_incomplete": True,
    }


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
        "top_genres": _top(genres, 10),
        "top_artists": _top(artists),
        "top_decades": _top(decades),
        "score_buckets": [(label, buckets[label]) for label, _, _ in SCORE_BUCKETS],
        "top_ratings": [
            {
                "album_id": row.get("album_id"),
                "artist": row.get("artist") or "Nieznany artysta",
                "album": row.get("album") or "Nieznane wydanie",
                "score": score,
                "cover": row.get("cover"),
                "must_hear": must_hear_album(
                    row.get("aoty_score"),
                    row.get("aoty_ratings_count"),
                    row.get("critic_score"),
                    row.get("critic_reviews_count"),
                ),
            }
            for row, score in top_ratings
        ],
    }


def rating_distribution(
    username: str,
    rows: list[dict],
    track_rows: list[dict],
    category: str,
    *,
    category_label: str | None = None,
    year: int | None = None,
    genre: str | None = None,
    score_min: int | None = None,
    score_max: int | None = None,
    reviewed: bool | None = None,
    liked: bool | None = None,
    has_tracks: bool | None = None,
    artist: str | None = None,
) -> dict:
    """Build one AOTY-style distribution without reading outside SQLite."""

    category = str(category or "all")

    def normalized(value) -> str:
        return "".join(
            character
            for character in str(value or "").casefold()
            if character.isalnum()
        )

    if category == "all":
        selected = list(rows) + list(track_rows)
    elif category == "tracks":
        selected = list(track_rows)
    else:
        accepted_formats = {normalized(category), normalized(category_label)}
        selected = [
            row
            for row in rows
            if normalized(row.get("release_format")) in accepted_formats
        ]

    filtered = [
        row for row in filter_rating_rows(
            selected,
            release_year=year,
            genre=genre,
            score_min=score_min,
            score_max=score_max,
            reviewed=reviewed,
            liked=liked,
            has_tracks=has_tracks,
            artist=artist,
        )
        if _score(row.get("score")) is not None
    ]

    summary = summarize(username, filtered)
    example_rows = filtered
    if category == "all":
        example_rows = [row for row in filtered if not row.get("_track_score")]

    def example(row: dict) -> dict:
        is_track = bool(row.get("_track_score"))
        return {
            "album_id": row.get("album_id"),
            "artist": row.get("artist") or "Nieznany artysta",
            "album": row.get("album") or "Nieznane wydanie",
            "title": (
                row.get("track_title") if is_track else row.get("album")
            ) or "Nieznana pozycja",
            "score": _score(row.get("score")),
            "cover": row.get("cover"),
            "is_track": is_track,
            "must_hear": must_hear_album(
                row.get("aoty_score"),
                row.get("aoty_ratings_count"),
                row.get("critic_score"),
                row.get("critic_reviews_count"),
            ),
        }

    ranked = sorted(
        example_rows,
        key=lambda row: (
            -(_score(row.get("score")) or 0),
            str(row.get("artist") or "").casefold(),
            str(row.get("album") or "").casefold(),
        ),
    )
    return {
        "username": username,
        "category": category,
        "label": category_label or category,
        "ratings": summary["ratings"],
        "average": summary["average"],
        "median": summary["median"],
        "score_buckets": summary["score_buckets"],
        "best_examples": [example(row) for row in ranked[:2]],
        "worst_examples": [example(row) for row in reversed(ranked[-2:])],
        "year": year,
        "genre": genre,
        "score_min": score_min,
        "score_max": score_max,
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
                "cover": row_a.get("cover") or row_b.get("cover"),
                "must_hear": bool(
                    row_a.get("must_hear")
                    or row_b.get("must_hear")
                    or must_hear_album(
                        row_a.get("aoty_score") or row_b.get("aoty_score"),
                        row_a.get("aoty_ratings_count")
                        or row_b.get("aoty_ratings_count"),
                        row_a.get("critic_score") or row_b.get("critic_score"),
                        row_a.get("critic_reviews_count")
                        or row_b.get("critic_reviews_count"),
                    )
                ),
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

    scores_a = [value[1] for value in map_a.values()]
    scores_b = [value[1] for value in map_b.values()]
    disagreements = sorted(
        common,
        key=lambda item: (-item["gap"], -item["mean"], item["album"].casefold()),
    )
    return {
        "user_a": user_a,
        "user_b": user_b,
        "ratings_a": len(map_a),
        "ratings_b": len(map_b),
        "average_a": statistics.fmean(scores_a) if scores_a else None,
        "average_b": statistics.fmean(scores_b) if scores_b else None,
        "median_a": statistics.median(scores_a) if scores_a else None,
        "median_b": statistics.median(scores_b) if scores_b else None,
        "common_count": len(common),
        "mean_gap": statistics.fmean(gaps) if gaps else None,
        "agreement": max(0.0, 100.0 - statistics.fmean(gaps)) if gaps else None,
        "disagreements": disagreements[:5],
        "ahead_a": [
            item for item in disagreements if item["score_a"] > item["score_b"]
        ][:3],
        "ahead_b": [
            item for item in disagreements if item["score_b"] > item["score_a"]
        ][:3],
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
