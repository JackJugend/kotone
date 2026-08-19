"""Centralna konfiguracja Kotone.

Ten moduł jest jedynym miejscem, które czyta config.json i wylicza ścieżki.
Dzięki temu każda komenda korzysta z dokładnie tych samych ustawień,
formatów i assetów.
"""

from __future__ import annotations

import json
import os

from formats import (
    DEFAULT_RATING_FETCH_LIMITS,
    RATING_FORMATS,
    build_rating_fetch_limits,
)

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
LASTFM_SOURCE_EMOJI = "<:lastfm:1539689853506293760>"
MUST_HEAR_USERS_EMOJI = "<:musthear_users:1539713390820458566>"
MUST_HEAR_CRITICS_EMOJI = "<:musthear_critics:1539713389557841981>"
MUST_HEAR_BOTH_EMOJI = "<:musthear_both:1539713387150319679>"

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


_LEGACY_USERS = (
    _validate_users(CONFIG.get("users"))
    if CONFIG.get("users") is not None
    else []
)
_DEFAULT_CHANNEL_ID = int(CONFIG["channel_id"])


def _validate_kotone_users(raw_profiles: object) -> dict[str, dict[str, object]]:
    """Validate optional Discord/AOTY/Last.fm identities kept in config.json.

    A profile with an AOTY username belongs to the strict persistence
    allow-list. A member without one (for example Gan) may still have a
    Discord and Last.fm identity, but is never scraped as an AOTY user.
    """

    if raw_profiles is None:
        raw_profiles = {
            "enso": {
                "discord_id": 805601151366070292,
                "aoty_username": "enso",
            },
            "kulkien": {
                "discord_id": 463642066401099786,
                "aoty_username": "kulkien",
            },
        }
    if not isinstance(raw_profiles, dict):
        raise RuntimeError("config.json -> kotone_users musi być mapą profili.")

    configured_aoty = {username.casefold(): username for username in _LEGACY_USERS}
    result: dict[str, dict[str, object]] = {}
    discord_ids: set[int] = set()
    aoty_names: set[str] = set()

    for raw_name, raw_profile in raw_profiles.items():
        name = str(raw_name or "").strip()
        if not name or not isinstance(raw_profile, dict):
            raise RuntimeError("config.json -> kotone_users zawiera niepoprawny profil.")
        key = name.casefold()
        if key in result:
            raise RuntimeError("config.json -> kotone_users zawiera zduplikowaną nazwę.")

        try:
            discord_id = int(raw_profile.get("discord_id") or 0)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"config.json -> {name}: niepoprawne discord_id.") from exc
        if discord_id <= 0 or discord_id in discord_ids:
            raise RuntimeError(f"config.json -> {name}: discord_id musi być unikalne.")

        aoty_raw = str(raw_profile.get("aoty_username") or "").strip()
        if (
            aoty_raw
            and _LEGACY_USERS
            and aoty_raw.casefold() not in configured_aoty
        ):
            raise RuntimeError(
                f"config.json -> {name}: aoty_username musi należeć do users."
            )
        aoty_username = (
            configured_aoty.get(aoty_raw.casefold(), aoty_raw)
            if aoty_raw
            else None
        )
        if aoty_username and aoty_username.casefold() in aoty_names:
            raise RuntimeError("config.json -> dwa profile wskazują ten sam AOTY user.")

        lastfm_username = str(raw_profile.get("lastfm_username") or "").strip() or None
        try:
            channel_id = int(raw_profile.get("channel_id") or _DEFAULT_CHANNEL_ID)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"config.json -> {name}: niepoprawne channel_id.") from exc
        if channel_id <= 0:
            raise RuntimeError(f"config.json -> {name}: channel_id musi być dodatnie.")
        result[key] = {
            "name": name,
            "discord_id": discord_id,
            "aoty_username": aoty_username,
            "lastfm_username": lastfm_username,
            "channel_id": channel_id,
        }
        discord_ids.add(discord_id)
        if aoty_username:
            aoty_names.add(aoty_username.casefold())

    missing_profiles = set(configured_aoty) - aoty_names
    if missing_profiles:
        raise RuntimeError(
            "config.json -> kotone_users nie zawiera profilu dla: "
            + ", ".join(sorted(missing_profiles))
        )
    return result


KOTONE_USERS = _validate_kotone_users(CONFIG.get("kotone_users"))
USERS = [
    str(profile["aoty_username"])
    for profile in KOTONE_USERS.values()
    if profile.get("aoty_username")
]
if not USERS:
    raise RuntimeError("config.json -> kotone_users musi zawierać co najmniej jeden aoty_username.")
KOTONE_USERS_BY_DISCORD_ID = {
    int(profile["discord_id"]): dict(profile)
    for profile in KOTONE_USERS.values()
}
KOTONE_USERS_BY_AOTY = {
    str(profile["aoty_username"]).casefold(): dict(profile)
    for profile in KOTONE_USERS.values()
    if profile.get("aoty_username")
}


def _validate_operators(raw_operators: object) -> dict[str, dict[str, object]]:
    """Resolve short profile names from config into Discord authorization data.

    ``operators`` deliberately contains profile keys (for example ``"enso"``),
    not copied numeric IDs.  This keeps all identities in ``kotone_users`` and
    makes adding/removing an administrator a one-line, auditable config edit.
    """

    if raw_operators is None:
        raw_operators = ["enso"]
    if not isinstance(raw_operators, list):
        raise RuntimeError("config.json -> operators musi być listą profili.")

    result: dict[str, dict[str, object]] = {}
    for raw_name in raw_operators:
        key = str(raw_name or "").strip().casefold()
        profile = KOTONE_USERS.get(key)
        if profile is None:
            raise RuntimeError(
                "config.json -> operators wskazuje nieznany profil: "
                f"{raw_name!r}."
            )
        result[key] = dict(profile)

    if not result:
        raise RuntimeError("config.json -> operators nie może być puste.")
    return result


OPERATORS = _validate_operators(CONFIG.get("operators"))
OPERATOR_DISCORD_IDS = frozenset(
    int(profile["discord_id"])
    for profile in OPERATORS.values()
)


def is_operator_discord_id(discord_id: object) -> bool:
    """Return whether a Discord account may run an administrative command."""

    try:
        return int(discord_id) in OPERATOR_DISCORD_IDS
    except (TypeError, ValueError):
        return False


LASTFM_API_KEY = str(os.getenv("LASTFM_API_KEY") or "").strip()
LASTFM_API_ENABLED = bool(LASTFM_API_KEY)
TOKEN = os.getenv("DISCORD_TOKEN") or CONFIG.get("discord_token", "")

if not TOKEN:
    raise RuntimeError(
        "Brak tokenu Discord. Ustaw DISCORD_TOKEN albo discord_token w config.json."
    )

APPLICATION_ID = int(CONFIG["application_id"])
GUILD_ID = int(CONFIG["guild_id"])
CHANNEL_ID = _DEFAULT_CHANNEL_ID

USER_CHANNELS = {
    str(profile["aoty_username"]).casefold(): int(profile["channel_id"])
    for profile in KOTONE_USERS.values()
    if profile.get("aoty_username")
}

# Mapowanie Discord ID -> własny profil AOTY jest polityką dostępu, a nie
# szczegółem komendy /import lub /manual. Trzymamy je przy pozostałej
# konfiguracji, aby obie komendy zawsze korzystały z dokładnie tej samej listy.
_DEFAULT_IMPORT_USERS_BY_DISCORD_ID = {
    int(profile["discord_id"]): str(profile["aoty_username"])
    for profile in KOTONE_USERS.values()
    if profile.get("aoty_username")
}


def _validate_import_users(raw_mapping: object) -> dict[int, str]:
    if not isinstance(raw_mapping, dict):
        raise RuntimeError(
            "config.json -> import_users_by_discord_id musi być mapą Discord ID -> user AOTY."
        )

    known_users = {user.casefold(): user for user in USERS}
    result: dict[int, str] = {}
    for raw_id, raw_username in raw_mapping.items():
        try:
            discord_id = int(raw_id)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "config.json -> import_users_by_discord_id zawiera niepoprawne Discord ID."
            ) from exc
        username = known_users.get(str(raw_username or "").strip().casefold())
        if discord_id <= 0 or username is None:
            raise RuntimeError(
                "config.json -> import_users_by_discord_id może wskazywać wyłącznie userów z users."
            )
        result[discord_id] = username
    return result


IMPORT_USERS_BY_DISCORD_ID = _validate_import_users(
    CONFIG.get(
        "import_users_by_discord_id",
        _DEFAULT_IMPORT_USERS_BY_DISCORD_ID,
    )
)

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

# /artist may fetch a public Last.fm picture only when SQLite has no current
# image. The result (including a failed lookup) is cached in SQLite.
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
# Independent public-provider switches.  Unlike ``/dbonly``'s historic AOTY
# state this file controls only optional fallback APIs, so an operator can
# pause Last.fm or MusicBrainz without silencing the AOTY monitor.
SOURCE_SWITCH_STATE_FILE = os.path.join(
    DATA_DIR or BASE_DIR,
    "source_switches.json",
)
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
    # MusicBrainz requires no more than one request per second.  The extra
    # margin avoids accidental boundary bursts from a shared Railway IP.
    "musicbrainz_min_request_interval", 1.25, 1.0
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

# Last.fm does not publish a fixed public quota and explicitly forbids
# continuous multi-request bursts. Kotone therefore uses a stricter local
# policy than MusicBrainz: one request at a time, at least two seconds apart,
# with a shared outage pause after a rate-limit or server failure.
LASTFM_MIN_REQUEST_INTERVAL = _runtime_float(
    "lastfm_min_request_interval", 2.0, 1.0
)
LASTFM_OUTAGE_COOLDOWN = _runtime_float(
    "lastfm_outage_cooldown", 15 * 60.0, 60.0
)
ARTIST_SOURCE_TTL = _runtime_int(
    "artist_source_ttl", 30 * 24 * 60 * 60, 24 * 60 * 60
)
LASTFM_RELEASE_SOURCE_TTL = _runtime_int(
    "lastfm_release_source_ttl", 30 * 24 * 60 * 60, 24 * 60 * 60
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

# Kompatybilne eksporty pozostają tutaj, bo zewnętrzne skrypty mogły dotąd
# importować je z settings.py. Nowy kod powinien importować katalog z formats.
RATING_FETCH_LIMITS = build_rating_fetch_limits(
    CONFIG.get("rating_fetch_limits", {})
)

ALBUM_LOOKUP_FALLBACK_LIMIT = max(
    20,
    int(CONFIG.get("album_lookup_fallback_limit", 300)),
)
