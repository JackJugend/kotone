"""Validation helpers for official AOTY ratings CSV exports."""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation


REQUIRED_COLUMNS = (
    "Artist",
    "Album",
    "Year",
    "Type",
    "Rating",
    "Date Rated",
)
MAX_IMPORT_ROWS = 10_000


class RatingImportError(ValueError):
    """The attachment is not a valid official AOTY ratings export."""


def normalized_text(value) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.translate(
        str.maketrans(
            {
                "–": "-",
                "—": "-",
                "−": "-",
                "’": "'",
                "‘": "'",
                "“": '"',
                "”": '"',
            }
        )
    )
    return re.sub(r"\s+", " ", text).strip().casefold()


def normalized_release_title(value) -> str:
    """Return a conservative comparison key for an album title.

    AOTY's export occasionally omits purely decorative wrapping characters
    present in the release page title (for example ``<K>`` -> ``K``).  They
    must not prevent an otherwise exact import match, but the original title
    remains untouched everywhere it is displayed or persisted.
    """

    text = normalized_text(value)
    if len(text) >= 2 and text[0] in "<[{(" and text[-1] in ">]})":
        inner = text[1:-1].strip()
        if inner:
            return inner
    return text


def normalized_identity(artist, album) -> tuple[str, str]:
    return normalized_text(artist), normalized_release_title(album)


def normalized_format(value) -> str:
    return normalized_text(value).replace("_", " ").replace("-", " ")


KNOWN_RELEASE_HINTS = {
    (
        *normalized_identity("tripleS", "World Wild Women"),
        2026,
        normalized_format("Single"),
    ): {
        "album_id": "1981558",
        "album_url": (
            "https://www.albumoftheyear.org/album/"
            "1981558-triples-world-wild-women.php"
        ),
        "release_details": {
            "artist": "tripleS",
            "album": "World Wild Women",
            "cover": (
                "https://cdn.albumoftheyear.org/album/"
                "1981558-world-wild-women_103735.jpg"
            ),
            "user_score": "71",
            "ratings_count": "20",
            "release_date": "August 17, 2026",
            "year": 2026,
            "album_format": "Single",
            "label": "Modhaus",
            "labels": ["Modhaus"],
            "genres": ["K-Pop", "Contemporary R&B"],
            "secondary_genres": ["New Jack Swing"],
            "source": "aoty",
            "_section_complete": {
                "score": True,
                "release_date": True,
                "format": True,
                "labels": True,
                "genres": True,
                "vibes": False,
                "ranking": False,
                "tracklist": False,
            },
        },
    },
    (
        *normalized_identity("NCT", "Golden Age"),
        2023,
        normalized_format("Single"),
    ): {
        "album_id": "732107",
        "album_url": "https://www.albumoftheyear.org/album/732107-nct-golden-age.php",
    },
    (
        *normalized_identity("NCT", "Golden Age"),
        2023,
        normalized_format("LP"),
    ): {
        "album_id": "722118",
        "album_url": "https://www.albumoftheyear.org/album/722118-nct-golden-age.php",
    },
}


def _score(value: str) -> str:
    try:
        score = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise RatingImportError("ocena nie jest liczbą") from None
    if not score.is_finite() or score < 0 or score > 100:
        raise RatingImportError("ocena musi mieścić się w zakresie 0–100")
    if score == score.to_integral():
        return str(int(score))
    return format(score.normalize(), "f")


def _year(value: str) -> int | None:
    value = str(value or "").strip()
    if not value:
        return None
    try:
        year = int(value)
    except ValueError:
        raise RatingImportError("rok nie jest liczbą") from None
    if year < 1800 or year > 2200:
        raise RatingImportError("rok jest poza obsługiwanym zakresem")
    return year


def _rating_date(value: str, row_index: int) -> tuple[str, float]:
    try:
        parsed = datetime.strptime(str(value).strip(), "%Y-%m-%d")
    except ValueError:
        raise RatingImportError("data musi mieć format RRRR-MM-DD") from None
    parsed = parsed.replace(tzinfo=timezone.utc)
    # Official exports are newest-first but contain no time of day. Preserve
    # that order for genuinely new rows without replacing precise saved times.
    return parsed.strftime("%d.%m.%Y"), parsed.timestamp() + max(0, 86_399 - row_index)


def parse_aoty_ratings_csv(payload: bytes) -> dict:
    if not payload:
        raise RatingImportError("plik jest pusty")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise RatingImportError("plik nie jest zapisany jako UTF-8 CSV") from None

    reader = csv.DictReader(io.StringIO(text, newline=""))
    headers = tuple(reader.fieldnames or ())
    missing = [column for column in REQUIRED_COLUMNS if column not in headers]
    if missing:
        raise RatingImportError(
            "brak wymaganych kolumn: " + ", ".join(missing)
        )

    rows: list[dict] = []
    rejected: list[dict] = []
    seen: set[tuple] = set()
    duplicates = 0

    for row_index, raw in enumerate(reader, start=1):
        if row_index > MAX_IMPORT_ROWS:
            raise RatingImportError(
                f"plik przekracza limit {MAX_IMPORT_ROWS} rekordów"
            )
        try:
            artist = str(raw.get("Artist") or "").strip()
            album = str(raw.get("Album") or "").strip()
            release_format = str(raw.get("Type") or "").strip()
            if not artist or not album:
                raise RatingImportError("brak artysty lub tytułu")
            if not release_format:
                raise RatingImportError("brak typu wydania")
            year = _year(raw.get("Year"))
            rating = _score(raw.get("Rating"))
            rating_date, sort_timestamp = _rating_date(
                raw.get("Date Rated"),
                row_index,
            )
            duplicate_key = (
                *normalized_identity(artist, album),
                year,
                normalized_format(release_format),
            )
            if duplicate_key in seen:
                duplicates += 1
            else:
                seen.add(duplicate_key)
            record = {
                "source_row": row_index + 1,
                "artist": artist,
                "album": album,
                "year": year,
                "release_format": release_format,
                "score": rating,
                "date": rating_date,
                "sort_timestamp": sort_timestamp,
            }
            hint = KNOWN_RELEASE_HINTS.get(
                (
                    *normalized_identity(artist, album),
                    year,
                    normalized_format(release_format),
                )
            )
            if hint:
                record["album_id_hint"] = hint["album_id"]
                record["album_url_hint"] = hint["album_url"]
                if hint.get("release_details"):
                    record["release_details_hint"] = dict(
                        hint["release_details"]
                    )
            rows.append(record)
        except RatingImportError as exc:
            rejected.append(
                {
                    "source_row": row_index + 1,
                    "artist": str(raw.get("Artist") or "").strip(),
                    "album": str(raw.get("Album") or "").strip(),
                    "reason": str(exc),
                }
            )

    if not rows:
        raise RatingImportError("plik nie zawiera żadnych poprawnych ocen")
    return {
        "rows": rows,
        "rejected": rejected,
        "duplicates": duplicates,
        "headers": headers,
    }


def unmatched_report_csv(items: list[dict]) -> bytes:
    output = io.StringIO(newline="")
    fields = (
        "source_row",
        "artist",
        "album",
        "year",
        "release_format",
        "score",
        "date",
        "reason",
    )
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()

    def safe(value):
        text = str(value if value is not None else "")
        return "'" + text if text.startswith(("=", "+", "-", "@")) else text

    for item in items:
        writer.writerow({field: safe(item.get(field)) for field in fields})
    return output.getvalue().encode("utf-8-sig")
