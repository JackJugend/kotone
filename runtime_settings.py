"""Interwały, limity i ścieżki stanu runtime Kotone.

Wszystkie wartości mają bezpieczne wartości domyślne i mogą być nadpisane
wyłącznie przez sekcję ``runtime`` w ``config.json``. Ten moduł nie zawiera
logiki użytkowników ani elementów UI.
"""

from __future__ import annotations

import os

from config_core import BASE_DIR, CONFIG, DATA_DIR
from formats import DEFAULT_RATING_FETCH_LIMITS, build_rating_fetch_limits


# ---------------------------------------------------------------------------
# Bezpieczne odczyty sekcji runtime
# ---------------------------------------------------------------------------

RUNTIME = (
    CONFIG.get("runtime", {})
    if isinstance(CONFIG.get("runtime", {}), dict)
    else {}
)


def _runtime_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(RUNTIME.get(name, default)))
    except (TypeError, ValueError):
        return max(minimum, int(default))


def _runtime_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(RUNTIME.get(name, default)))
    except (TypeError, ValueError):
        return max(minimum, float(default))


# ---------------------------------------------------------------------------
# Monitor, profile i archiwum
# ---------------------------------------------------------------------------

CHECK_INTERVAL = max(60, int(CONFIG.get("check_interval", 300)))
PROFILE_SYNC_INTERVAL = _runtime_int("profile_sync_interval", 30 * 60, 300)
AVATAR_AOTY_SYNC_INTERVAL = _runtime_int(
    "avatar_aoty_sync_interval",
    7 * 24 * 60 * 60,
    7 * 24 * 60 * 60,
)

FULL_SYNC_INTERVAL = _runtime_int("full_sync_interval", 6 * 60 * 60, 15 * 60)
QUICK_RATING_LIMIT_PER_FORMAT = _runtime_int(
    "quick_rating_limit_per_format",
    20,
    5,
)
# The root AOTY ratings route covers album-like formats. Singles and music
# videos live behind separate routes, therefore checking both on every monitor
# cycle triples traffic.  They are rotated by the monitor on this cadence.
QUICK_SPECIAL_CHECK_INTERVAL = _runtime_int(
    "quick_special_check_interval",
    60 * 60,
    5 * 60,
)

DETAIL_ENRICH_PER_CYCLE = _runtime_int("detail_enrich_per_cycle", 2, 0)
RELEASE_ENRICH_PER_CYCLE = _runtime_int("release_enrich_per_cycle", 2, 0)

PROFILE_RATING_ARCHIVE_INTERVAL = _runtime_int(
    "profile_rating_archive_interval",
    24 * 60 * 60,
    60 * 60,
)
PROFILE_RATING_ARCHIVE_FORMATS_PER_CYCLE = _runtime_int(
    "profile_rating_archive_formats_per_cycle",
    1,
    0,
)
PROFILE_RATING_ARCHIVE_LIMIT_PER_FORMAT = _runtime_int(
    "profile_rating_archive_limit_per_format",
    0,
    0,
)

ARCHIVE_WORKER_START_DELAY = _runtime_float(
    "archive_worker_start_delay",
    15.0,
    0.0,
)
ARCHIVE_WORKER_REST_SECONDS = _runtime_float(
    "archive_worker_rest_seconds",
    4.0,
    1.0,
)
ENRICH_WORKER_REST_SECONDS = _runtime_float(
    "enrich_worker_rest_seconds",
    12.0,
    2.0,
)
ARCHIVE_WORKER_ERROR_SLEEP = _runtime_float(
    "archive_worker_error_sleep",
    5 * 60.0,
    30.0,
)
ARCHIVE_WORKER_IDLE_SECONDS = _runtime_float(
    "archive_worker_idle_seconds",
    5 * 60.0,
    30.0,
)


# ---------------------------------------------------------------------------
# AOTY
# ---------------------------------------------------------------------------

AOTY_ARCHIVE_MAX_PAGES = _runtime_int("aoty_archive_max_pages", 500, 20)
AOTY_MIN_REQUEST_INTERVAL = _runtime_float(
    "aoty_min_request_interval",
    1.25,
    0.2,
)
AOTY_MAINTENANCE_MIN_REQUEST_INTERVAL = _runtime_float(
    "aoty_maintenance_min_request_interval",
    2.0,
    AOTY_MIN_REQUEST_INTERVAL,
)
AOTY_MAX_RETRIES = _runtime_int("aoty_max_retries", 2, 0)
AOTY_CIRCUIT_FAILURES = _runtime_int("aoty_circuit_failures", 4, 2)
AOTY_CIRCUIT_COOLDOWN = _runtime_float(
    "aoty_circuit_cooldown",
    90.0,
    10.0,
)
AOTY_CHALLENGE_COOLDOWN = _runtime_float(
    "aoty_challenge_cooldown",
    2 * 60 * 60.0,
    5 * 60.0,
)

AOTY_CHALLENGE_STATE_FILE = os.path.join(
    DATA_DIR or BASE_DIR,
    "aoty_challenge_state.json",
)
AOTY_DB_ONLY_STATE_FILE = os.path.join(
    DATA_DIR or BASE_DIR,
    "aoty_db_only_state.json",
)
SOURCE_SWITCH_STATE_FILE = os.path.join(
    DATA_DIR or BASE_DIR,
    "source_switches.json",
)

AOTY_CACHE_MAX_ENTRIES = _runtime_int("aoty_cache_max_entries", 512, 32)
AOTY_REQUEST_TIMEOUT_CONNECT = _runtime_float(
    "aoty_connect_timeout",
    8.0,
    2.0,
)
AOTY_REQUEST_TIMEOUT_READ = _runtime_float(
    "aoty_read_timeout",
    25.0,
    5.0,
)

RATING_DETAIL_TTL = _runtime_int("rating_detail_ttl", 60 * 60, 5 * 60)
DETAIL_CHANGE_SCAN_INTERVAL = _runtime_int(
    "detail_change_scan_interval",
    12 * 60 * 60,
    60 * 60,
)
RELEASE_DETAIL_TTL = _runtime_int(
    "release_detail_ttl",
    12 * 60 * 60,
    10 * 60,
)


# ---------------------------------------------------------------------------
# MusicBrainz
# ---------------------------------------------------------------------------

MUSICBRAINZ_FALLBACK_ENABLED = bool(
    RUNTIME.get("musicbrainz_fallback_enabled", True)
)
MUSICBRAINZ_MIN_REQUEST_INTERVAL = _runtime_float(
    "musicbrainz_min_request_interval",
    1.25,
    1.0,
)
MUSICBRAINZ_REQUEST_TIMEOUT = _runtime_float(
    "musicbrainz_request_timeout",
    15.0,
    3.0,
)
MUSICBRAINZ_OUTAGE_COOLDOWN = _runtime_float(
    "musicbrainz_outage_cooldown",
    15 * 60.0,
    60.0,
)
MUSICBRAINZ_MAX_OUTAGE_COOLDOWN = _runtime_float(
    "musicbrainz_max_outage_cooldown",
    6 * 60 * 60.0,
    MUSICBRAINZ_OUTAGE_COOLDOWN,
)
MUSICBRAINZ_STATE_FILE = os.path.join(
    DATA_DIR or BASE_DIR,
    "musicbrainz_state.json",
)
MUSICBRAINZ_FALLBACK_RETRY_INTERVAL = _runtime_int(
    "musicbrainz_fallback_retry_interval",
    24 * 60 * 60,
    60 * 60,
)


# ---------------------------------------------------------------------------
# Discogs
# ---------------------------------------------------------------------------

# Discogs is a deliberately narrow, low-priority fallback.  Kotone asks it
# only for a missing public tracklist or total release duration; it never
# reads ratings, reviews or other personal data from this provider.
DISCOGS_MIN_REQUEST_INTERVAL = _runtime_float(
    "discogs_min_request_interval",
    1.25,
    1.0,
)
DISCOGS_REQUEST_TIMEOUT = _runtime_float(
    "discogs_request_timeout",
    15.0,
    3.0,
)
DISCOGS_OUTAGE_COOLDOWN = _runtime_float(
    "discogs_outage_cooldown",
    15 * 60.0,
    60.0,
)
DISCOGS_MAX_OUTAGE_COOLDOWN = _runtime_float(
    "discogs_max_outage_cooldown",
    6 * 60 * 60.0,
    DISCOGS_OUTAGE_COOLDOWN,
)
DISCOGS_STATE_FILE = os.path.join(
    DATA_DIR or BASE_DIR,
    "discogs_state.json",
)

# Parse jest płatnym fallbackiem metadanych AOTY. Limit per użytkownik
# chroni kredyty: najwyżej jeden album dziennie dla każdego konta Kotone.
PARSE_REQUEST_TIMEOUT = _runtime_float("parse_request_timeout", 15.0, 3.0)
PARSE_USER_DAILY_INTERVAL = _runtime_int(
    "parse_user_daily_interval", 24 * 60 * 60, 60 * 60
)


# ---------------------------------------------------------------------------
# Last.fm
# ---------------------------------------------------------------------------

LASTFM_MIN_REQUEST_INTERVAL = _runtime_float(
    "lastfm_min_request_interval",
    2.0,
    1.0,
)
LASTFM_OUTAGE_COOLDOWN = _runtime_float(
    "lastfm_outage_cooldown",
    15 * 60.0,
    60.0,
)
ARTIST_SOURCE_TTL = _runtime_int(
    "artist_source_ttl",
    30 * 24 * 60 * 60,
    24 * 60 * 60,
)
LASTFM_RELEASE_SOURCE_TTL = _runtime_int(
    "lastfm_release_source_ttl",
    30 * 24 * 60 * 60,
    24 * 60 * 60,
)
LASTFM_ARTIST_IMAGE_TTL = _runtime_int(
    "lastfm_artist_image_ttl",
    30 * 24 * 60 * 60,
    60 * 60,
)
LASTFM_ARTIST_IMAGE_RETRY_INTERVAL = _runtime_int(
    "lastfm_artist_image_retry_interval",
    24 * 60 * 60,
    5 * 60,
)
LASTFM_PROFILE_SYNC_INTERVAL = _runtime_int(
    "lastfm_profile_sync_interval",
    6 * 60 * 60,
    15 * 60,
)
LASTFM_HISTORY_PAGE_SIZE = _runtime_int("lastfm_history_page_size", 200, 25)
LASTFM_HISTORY_PAGE_INTERVAL = _runtime_int(
    "lastfm_history_page_interval",
    20,
    5,
)
LASTFM_NEWEST_SCROBBLE_INTERVAL = _runtime_int(
    "lastfm_newest_scrobble_interval",
    6 * 60 * 60,
    30 * 60,
)


# ---------------------------------------------------------------------------
# SQLite, Railway i limity komend
# ---------------------------------------------------------------------------

LOCAL_DATABASE_BACKUP_INTERVAL = _runtime_int(
    "local_database_backup_interval",
    24 * 60 * 60,
    60 * 60,
)
DATABASE_BACKUP_FILE = os.path.join(
    DATA_DIR or BASE_DIR,
    "kotone.backup.sqlite3",
)
PORT = _runtime_int("port", int(os.getenv("PORT", "8080")), 1)

RATING_FETCH_LIMITS = build_rating_fetch_limits(
    CONFIG.get("rating_fetch_limits", DEFAULT_RATING_FETCH_LIMITS)
)
ALBUM_LOOKUP_FALLBACK_LIMIT = max(
    20,
    int(CONFIG.get("album_lookup_fallback_limit", 300)),
)
