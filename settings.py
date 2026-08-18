"""Centralna konfiguracja Kotone.

Ten moduł jest jedynym miejscem, które czyta config.json i wylicza ścieżki.
Dzięki temu każda komenda korzysta z dokładnie tych samych ustawień,
formatów i assetów.
"""

from __future__ import annotations

import json
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
BASE_URL = "https://www.albumoftheyear.org"

# Ikona AOTY do footerów embedów.
#
# Używamy publicznego faviconu AOTY zamiast attachment://aoty.jpg.
# Dzięki temu po przełączeniu embeda buttonem lokalny plik nie zostaje
# jako osobny, ogromny obrazek nad embedem.
AOTY_ICON = os.path.join(BASE_DIR, "assets", "aoty.jpg")
AOTY_ICON_FILENAME = "aoty.jpg"
AOTY_ICON_URL = "https://cdn.albumoftheyear.org/images/favicon.png"

# Zachowujemy starą nazwę zmiennej dla kompatybilności istniejących komend.
AOTY_ICON_ATTACHMENT = AOTY_ICON_URL
AOTY_SOURCE_EMOJI = "<:aoty:1539095897084924004>"
MUSICBRAINZ_SOURCE_EMOJI = "<:music_brainz:1539096206083629186>"

# Runtime state.
#
# SQLite jest teraz głównym magazynem stanu. data.json służy wyłącznie jako
# źródło jednorazowej migracji ze starszych wersji.
DEFAULT_DATA_FILE = os.path.join(BASE_DIR, "data.json")
DEFAULT_DATABASE_FILE = os.path.join(BASE_DIR, "kotone.sqlite3")

# Railway injects RAILWAY_VOLUME_MOUNT_PATH for an attached volume. Explicit
# DATA_DIR still wins locally/when intentionally overridden, but Kotone no
# longer depends on remembering a second Railway variable just to persist DB.
RAILWAY_VOLUME_DIR = os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
DATA_DIR = os.getenv("DATA_DIR") or RAILWAY_VOLUME_DIR

if DATA_DIR:
    os.makedirs(DATA_DIR, exist_ok=True)

    DATA_FILE = os.path.join(DATA_DIR, "data.json")
    DATABASE_FILE = os.path.join(DATA_DIR, "kotone.sqlite3")
    MIGRATED_DATA_BACKUP_FILE = os.path.join(
        DATA_DIR,
        "data_migrated.json.bak",
    )
else:
    DATA_FILE = DEFAULT_DATA_FILE
    DATABASE_FILE = DEFAULT_DATABASE_FILE
    MIGRATED_DATA_BACKUP_FILE = os.path.join(
        BASE_DIR,
        "data_migrated.json.bak",
    )

with open(CONFIG_FILE, "r", encoding="utf-8") as file:
    CONFIG = json.load(file)


def _validate_users(raw_users) -> list[str]:
    """Return the configured persistence allow-list or fail before DB startup.

    ``users`` controls a destructive startup prune, so permissive coercion is
    unsafe here.  In particular, iterating a JSON string would otherwise turn
    every character into a separate username and remove the real users from
    SQLite.
    """
    if not isinstance(raw_users, list) or not raw_users:
        raise RuntimeError(
            "config.json -> users musi być niepustą listą nazw użytkowników AOTY."
        )

    users: list[str] = []
    seen: set[str] = set()

    for position, raw_user in enumerate(raw_users, start=1):
        if not isinstance(raw_user, str) or not raw_user.strip():
            raise RuntimeError(
                "config.json -> users zawiera pustą lub niepoprawną nazwę "
                f"na pozycji {position}."
            )

        username = raw_user.strip()
        folded = username.casefold()
        if folded in seen:
            raise RuntimeError(
                "config.json -> users zawiera zduplikowaną nazwę "
                f"{username!r} (wielkość liter nie ma znaczenia)."
            )

        seen.add(folded)
        users.append(username)

    return users


USERS = _validate_users(CONFIG.get("users"))
TOKEN = os.getenv("DISCORD_TOKEN") or CONFIG.get("discord_token", "")

if not TOKEN:
    raise RuntimeError(
        "Brak tokenu Discord. Ustaw DISCORD_TOKEN albo discord_token w config.json."
    )

APPLICATION_ID = int(CONFIG["application_id"])
GUILD_ID = int(CONFIG["guild_id"])
CHANNEL_ID = int(CONFIG["channel_id"])

USER_CHANNELS = {
    str(username).casefold(): int(channel_id)
    for username, channel_id in CONFIG.get("user_channels", {}).items()
}

CHECK_INTERVAL = max(60, int(CONFIG.get("check_interval", 300)))

# ---------------------------------------------------------------------------
# Runtime / reliability settings
# ---------------------------------------------------------------------------
#
# Wszystkie wartości mają bezpieczne defaulty. Można je nadpisać w config.json
# w sekcji "runtime" bez dotykania kodu. Dzięki temu tuning Railway/AOTY nie
# wymaga kolejnego refactoru.
RUNTIME = CONFIG.get("runtime", {}) if isinstance(CONFIG.get("runtime", {}), dict) else {}

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

# Profil zmienia się dużo rzadziej niż ratings, więc odświeżamy go osobno.
PROFILE_SYNC_INTERVAL = _runtime_int("profile_sync_interval", 30 * 60, 300)

# Quick sync służy do wykrywania nowych/recent zmian. Pełny sync co kilka
# godzin łapie zmianę starej oceny, której quick sync już nie widzi.
FULL_SYNC_INTERVAL = _runtime_int("full_sync_interval", 6 * 60 * 60, 15 * 60)
QUICK_RATING_LIMIT_PER_FORMAT = _runtime_int("quick_rating_limit_per_format", 20, 5)

# Background enrichment stopniowo zapisuje review/track ratings i publiczne
# szczegóły wydań do SQLite bez robienia burstu requestów.
DETAIL_ENRICH_PER_CYCLE = _runtime_int("detail_enrich_per_cycle", 2, 0)
RELEASE_ENRICH_PER_CYCLE = _runtime_int("release_enrich_per_cycle", 2, 0)

# Niezależny archiwizator profilu. Monitor może celowo ignorować niektóre
# formaty (limit 0), ale baza ma docelowo znać pełny profil użytkowników z
# config. Dlatego po cichu odświeżamy po jednym/kilku formatach na cykl.
# Każdy format ma własny timestamp, więc praca rozkłada się w czasie zamiast
# robić kilkadziesiąt requestów naraz.
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
# 0 = bez limitu liczby ocen w jednym formacie. Archiwizator nadal ma
# techniczny limit stron (ochrona przed zapętloną paginacją AOTY), ale nie
# ucina profilu po arbitralnej liczbie ratings.
PROFILE_RATING_ARCHIVE_LIMIT_PER_FORMAT = _runtime_int(
    "profile_rating_archive_limit_per_format",
    0,
    0,
)

# Osobny worker archiwum działa niezależnie od 20-minutowego monitora.
# Dzięki temu pierwszy pełny zapis profili nie czeka jednego cyklu na każdy
# format, a zwykły monitor nie jest blokowany wielostronicowym scrapowaniem.
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
AOTY_ARCHIVE_MAX_PAGES = _runtime_int(
    "aoty_archive_max_pages",
    500,
    20,
)

# Centralny transport AOTY. Jeden request naraz + minimalny odstęp to
# najważniejsza ochrona przed 429.
AOTY_MIN_REQUEST_INTERVAL = _runtime_float("aoty_min_request_interval", 1.25, 0.2)
AOTY_MAINTENANCE_MIN_REQUEST_INTERVAL = _runtime_float(
    "aoty_maintenance_min_request_interval",
    2.0,
    AOTY_MIN_REQUEST_INTERVAL,
)
AOTY_MAX_RETRIES = _runtime_int("aoty_max_retries", 2, 0)
AOTY_CIRCUIT_FAILURES = _runtime_int("aoty_circuit_failures", 4, 2)
AOTY_CIRCUIT_COOLDOWN = _runtime_float("aoty_circuit_cooldown", 90.0, 10.0)
# A challenge/interstitial is a valid HTTP response but not a usable AOTY page.
# One sighting therefore pauses every shared scraper path for two hours instead
# of letting the monitor and maintenance worker keep the protection alive.
AOTY_CHALLENGE_COOLDOWN = _runtime_float(
    "aoty_challenge_cooldown",
    2 * 60 * 60.0,
    5 * 60.0,
)
# Persisted on the Railway volume.  Without this a deploy forgets an active
# anti-bot challenge and immediately probes AOTY again, effectively starting
# a fresh cooldown every time the service restarts.
AOTY_CHALLENGE_STATE_FILE = os.path.join(
    DATA_DIR or BASE_DIR,
    "aoty_challenge_state.json",
)
# Operator-controlled hard pause.  This lives beside the challenge cooldown on
# the persistent Railway volume, so toggling /dbonly never requires a deploy.
AOTY_DB_ONLY_STATE_FILE = os.path.join(
    DATA_DIR or BASE_DIR,
    "aoty_db_only_state.json",
)
# Stable Discord user ID allowed to toggle the global AOTY pause. A username
# is mutable, so authorization must never depend on a display name.
try:
    AOTY_DB_ONLY_ADMIN_USER_ID = int(
        CONFIG.get("aoty_db_only_admin_user_id", 805601151366070292)
    )
except (TypeError, ValueError):
    AOTY_DB_ONLY_ADMIN_USER_ID = 805601151366070292
AOTY_CACHE_MAX_ENTRIES = _runtime_int("aoty_cache_max_entries", 512, 32)
AOTY_REQUEST_TIMEOUT_CONNECT = _runtime_float("aoty_connect_timeout", 8.0, 2.0)
AOTY_REQUEST_TIMEOUT_READ = _runtime_float("aoty_read_timeout", 25.0, 5.0)

# Po jakim czasie cached detail konkretnego ratingu warto sprawdzić ponownie.
RATING_DETAIL_TTL = _runtime_int("rating_detail_ttl", 60 * 60, 5 * 60)

# Background change tracker revisits already-known reviews/likes/Track Ratings
# much more slowly than interactive cache TTL. Dirty card/detail mismatches are
# still checked immediately; this interval only controls periodic verification.
DETAIL_CHANGE_SCAN_INTERVAL = _runtime_int(
    "detail_change_scan_interval",
    12 * 60 * 60,
    60 * 60,
)

RELEASE_DETAIL_TTL = _runtime_int("release_detail_ttl", 12 * 60 * 60, 10 * 60)

# MusicBrainz is an official, read-only fallback for public release metadata
# when AOTY is unavailable.  It is used only by the low-priority worker, never
# while rendering a Discord command.
MUSICBRAINZ_FALLBACK_ENABLED = bool(
    RUNTIME.get("musicbrainz_fallback_enabled", True)
)
MUSICBRAINZ_MIN_REQUEST_INTERVAL = _runtime_float(
    "musicbrainz_min_request_interval", 1.05, 1.0
)
MUSICBRAINZ_REQUEST_TIMEOUT = _runtime_float(
    "musicbrainz_request_timeout", 15.0, 3.0
)
MUSICBRAINZ_OUTAGE_COOLDOWN = _runtime_float(
    "musicbrainz_outage_cooldown", 15 * 60.0, 60.0
)
MUSICBRAINZ_FALLBACK_RETRY_INTERVAL = _runtime_int(
    "musicbrainz_fallback_retry_interval", 24 * 60 * 60, 60 * 60
)

# Lokalny backup SQLite na tym samym volume. Railway backups nadal są mocno
# zalecane, ale ten plik daje dodatkową warstwę ochrony przed uszkodzeniem DB.
LOCAL_DATABASE_BACKUP_INTERVAL = _runtime_int("local_database_backup_interval", 24 * 60 * 60, 60 * 60)
DATABASE_BACKUP_FILE = os.path.join(
    DATA_DIR or BASE_DIR,
    "kotone.backup.sqlite3",
)

# Health server dla Railway. Nie odpytuje AOTY — stan zewnętrznego serwisu nie
# powinien decydować, czy nowy deploy Kotone jest zdrowy.
PORT = _runtime_int("port", int(os.getenv("PORT", "8080")), 1)

# Wszystkie formaty obsługiwane przez monitor /last /artist.
RATING_FORMATS = {
    "lp": {"slug": "lp", "label": "LP"},
    "ep": {"slug": "ep", "label": "EP"},
    "mixtape": {"slug": "mixtape", "label": "Mixtape"},
    "single": {"slug": "single", "label": "Single"},
    "compilation": {"slug": "compilation", "label": "Compilation"},
    "live": {"slug": "live", "label": "Live"},
    "reissue": {"slug": "reissue", "label": "Reissue"},
    "soundtrack": {"slug": "soundtrack", "label": "Soundtrack"},
    "holiday": {"slug": "holiday", "label": "Holiday"},
    "dj_mix": {"slug": "dj-mix", "label": "DJ Mix"},
    "box_set": {"slug": "box-set", "label": "Box Set"},
    "instrumental": {"slug": "instrumental", "label": "Instrumental"},
    "unofficial": {"slug": "unofficial", "label": "Unofficial"},
    "video": {"slug": "video", "label": "Video"},
    "demo": {"slug": "demo", "label": "Demo"},
    "miscellaneous": {"slug": "miscellaneous", "label": "Miscellaneous"},
    "music_video": {"slug": "music-video", "label": "Music Video"},
    "remix": {"slug": "remix", "label": "Remix"},
    "audiobook": {"slug": "audiobook", "label": "Audiobook"},
}

DEFAULT_RATING_FETCH_LIMITS = {
    "lp": 120,
    "ep": 60,
    "mixtape": 60,
    "single": 60,
    "compilation": 30,
    "live": 20,
    "reissue": 20,
    "soundtrack": 20,
    "holiday": 0,
    "dj_mix": 0,
    "box_set": 0,
    "instrumental": 0,
    "unofficial": 0,
    "video": 0,
    "demo": 0,
    "miscellaneous": 0,
    "music_video": 20,
    "remix": 0,
    "audiobook": 0,
}

raw_limits = CONFIG.get("rating_fetch_limits", {})
RATING_FETCH_LIMITS: dict[str, int] = {}

for key, info in RATING_FORMATS.items():
    default = DEFAULT_RATING_FETCH_LIMITS.get(key, 0)
    raw = raw_limits.get(key, raw_limits.get(info["slug"], default))

    try:
        value = max(0, int(raw))
    except (TypeError, ValueError):
        value = default

    RATING_FETCH_LIMITS[key] = value

ALBUM_LOOKUP_FALLBACK_LIMIT = max(
    20,
    int(CONFIG.get("album_lookup_fallback_limit", 300)),
)
