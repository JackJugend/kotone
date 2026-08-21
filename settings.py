"""Publiczna konfiguracja Kotone.

Układ modułów konfiguracyjnych jest celowo prosty:

* ``config_core`` — config.json i ścieżki danych,
* ``ui_constants`` — assety, emoji i symbole interfejsu,
* ``artist_aliases`` — ręcznie zweryfikowane aliasy,
* ``runtime_settings`` — interwały, limity i pliki stanu,
* ten moduł — użytkownicy, operatorzy i dane logowania Discord/Last.fm.

Pozostały kod może nadal importować wszystko z ``settings``. Ten plik jest
stabilną fasadą i nie miesza już stałych UI z walidacją oraz tuningiem runtime.
"""

from __future__ import annotations

import os

from artist_aliases import ARTIST_ALIAS_GROUPS, artist_alias_variants
from config_core import (
    BASE_DIR,
    BASE_URL,
    CONFIG,
    CONFIG_FILE,
    DATABASE_FILE,
    DATA_DIR,
    DATA_FILE,
    DEFAULT_DATABASE_FILE,
    DEFAULT_DATA_FILE,
    LASTFM_DATABASE_FILE,
    MIGRATED_DATA_BACKUP_FILE,
    RAILWAY_VOLUME_DIR,
)
from formats import RATING_FORMATS
from runtime_settings import *  # noqa: F403 - publiczna fasada kompatybilności
from ui_constants import *  # noqa: F403 - publiczna fasada kompatybilności


# ---------------------------------------------------------------------------
# Walidacja użytkowników Kotone
# ---------------------------------------------------------------------------

def _validate_users(raw_users: object) -> list[str]:
    """Zwróć bezpieczną allow-listę AOTY przed uruchomieniem bazy."""

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


def _validate_kotone_users(
    raw_profiles: object,
    *,
    legacy_users: list[str],
    default_channel_id: int,
) -> dict[str, dict[str, object]]:
    """Sprawdź wspólne tożsamości Discord/AOTY/Last.fm z config.json."""

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

    configured_aoty = {username.casefold(): username for username in legacy_users}
    result: dict[str, dict[str, object]] = {}
    discord_ids: set[int] = set()
    aoty_names: set[str] = set()

    for raw_name, raw_profile in raw_profiles.items():
        name = str(raw_name or "").strip()
        if not name or not isinstance(raw_profile, dict):
            raise RuntimeError(
                "config.json -> kotone_users zawiera niepoprawny profil."
            )

        key = name.casefold()
        if key in result:
            raise RuntimeError(
                "config.json -> kotone_users zawiera zduplikowaną nazwę."
            )

        try:
            discord_id = int(raw_profile.get("discord_id") or 0)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"config.json -> {name}: niepoprawne discord_id."
            ) from exc
        if discord_id <= 0 or discord_id in discord_ids:
            raise RuntimeError(
                f"config.json -> {name}: discord_id musi być unikalne."
            )

        aoty_raw = str(raw_profile.get("aoty_username") or "").strip()
        if aoty_raw and legacy_users and aoty_raw.casefold() not in configured_aoty:
            raise RuntimeError(
                f"config.json -> {name}: aoty_username musi należeć do users."
            )
        aoty_username = (
            configured_aoty.get(aoty_raw.casefold(), aoty_raw)
            if aoty_raw
            else None
        )
        if aoty_username and aoty_username.casefold() in aoty_names:
            raise RuntimeError(
                "config.json -> dwa profile wskazują ten sam AOTY user."
            )

        lastfm_username = (
            str(raw_profile.get("lastfm_username") or "").strip() or None
        )
        try:
            channel_id = int(raw_profile.get("channel_id") or default_channel_id)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"config.json -> {name}: niepoprawne channel_id."
            ) from exc
        if channel_id <= 0:
            raise RuntimeError(
                f"config.json -> {name}: channel_id musi być dodatnie."
            )

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


def _validate_operators(
    raw_operators: object,
    kotone_users: dict[str, dict[str, object]],
) -> dict[str, dict[str, object]]:
    """Rozwiąż krótkie nazwy operatorów do jednego profilu Kotone."""

    if raw_operators is None:
        raw_operators = ["enso"]
    if not isinstance(raw_operators, list):
        raise RuntimeError("config.json -> operators musi być listą profili.")

    result: dict[str, dict[str, object]] = {}
    for raw_name in raw_operators:
        key = str(raw_name or "").strip().casefold()
        profile = kotone_users.get(key)
        if profile is None:
            raise RuntimeError(
                "config.json -> operators wskazuje nieznany profil: "
                f"{raw_name!r}."
            )
        result[key] = dict(profile)

    if not result:
        raise RuntimeError("config.json -> operators nie może być puste.")
    return result


def _validate_import_users(
    raw_mapping: object,
    users: list[str],
) -> dict[int, str]:
    """Sprawdź mapę Discord ID -> profil AOTY dla ręcznych importów."""

    if not isinstance(raw_mapping, dict):
        raise RuntimeError(
            "config.json -> import_users_by_discord_id musi być mapą "
            "Discord ID -> user AOTY."
        )

    known_users = {user.casefold(): user for user in users}
    result: dict[int, str] = {}
    for raw_id, raw_username in raw_mapping.items():
        try:
            discord_id = int(raw_id)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "config.json -> import_users_by_discord_id zawiera "
                "niepoprawne Discord ID."
            ) from exc

        username = known_users.get(str(raw_username or "").strip().casefold())
        if discord_id <= 0 or username is None:
            raise RuntimeError(
                "config.json -> import_users_by_discord_id może wskazywać "
                "wyłącznie userów z users."
            )
        result[discord_id] = username
    return result


# ---------------------------------------------------------------------------
# Zbudowane, globalne profile i mapy
# ---------------------------------------------------------------------------

_LEGACY_USERS = (
    _validate_users(CONFIG.get("users"))
    if CONFIG.get("users") is not None
    else []
)
_DEFAULT_CHANNEL_ID = int(CONFIG["channel_id"])

KOTONE_USERS = _validate_kotone_users(
    CONFIG.get("kotone_users"),
    legacy_users=_LEGACY_USERS,
    default_channel_id=_DEFAULT_CHANNEL_ID,
)
USERS = [
    str(profile["aoty_username"])
    for profile in KOTONE_USERS.values()
    if profile.get("aoty_username")
]
if not USERS:
    raise RuntimeError(
        "config.json -> kotone_users musi zawierać co najmniej jeden "
        "aoty_username."
    )

KOTONE_USERS_BY_DISCORD_ID = {
    int(profile["discord_id"]): dict(profile)
    for profile in KOTONE_USERS.values()
}
KOTONE_USERS_BY_AOTY = {
    str(profile["aoty_username"]).casefold(): dict(profile)
    for profile in KOTONE_USERS.values()
    if profile.get("aoty_username")
}
KOTONE_AVATAR_EMOJI_NAMES = {
    str(profile["aoty_username"]).casefold(): str(profile["name"]).casefold()
    for profile in KOTONE_USERS.values()
    if profile.get("aoty_username")
}
KOTONE_BOT_AVATAR_EMOJI_KEY = "kotone"
KOTONE_BOT_AVATAR_EMOJI_NAME = "kotone"

OPERATORS = _validate_operators(CONFIG.get("operators"), KOTONE_USERS)
OPERATOR_DISCORD_IDS = frozenset(
    int(profile["discord_id"])
    for profile in OPERATORS.values()
)

USER_CHANNELS = {
    str(profile["aoty_username"]).casefold(): int(profile["channel_id"])
    for profile in KOTONE_USERS.values()
    if profile.get("aoty_username")
}
_DEFAULT_IMPORT_USERS_BY_DISCORD_ID = {
    int(profile["discord_id"]): str(profile["aoty_username"])
    for profile in KOTONE_USERS.values()
    if profile.get("aoty_username")
}
IMPORT_USERS_BY_DISCORD_ID = _validate_import_users(
    CONFIG.get(
        "import_users_by_discord_id",
        _DEFAULT_IMPORT_USERS_BY_DISCORD_ID,
    ),
    USERS,
)


# ---------------------------------------------------------------------------
# Publiczne resolvery uprawnień i profili
# ---------------------------------------------------------------------------

def resolve_kotone_profile(
    discord_user_id: int | str | None,
    supplied_name: str | None,
) -> dict[str, object] | None:
    """Znajdź profil po Discord ID lub dowolnej skonfigurowanej nazwie."""

    supplied = str(supplied_name or "").strip().casefold()
    if supplied:
        for key, profile in KOTONE_USERS.items():
            aliases = {
                key,
                str(profile.get("name") or "").casefold(),
                str(profile.get("aoty_username") or "").casefold(),
                str(profile.get("lastfm_username") or "").casefold(),
            }
            if supplied in aliases:
                return dict(profile)
        return None

    try:
        profile = KOTONE_USERS_BY_DISCORD_ID.get(int(discord_user_id or 0))
    except (TypeError, ValueError):
        profile = None
    return dict(profile) if profile else None


def resolve_aoty_username(
    discord_user_id: int | str | None,
    supplied_username: str | None,
) -> str | None:
    """Użyj podanego AOTY usera albo profilu osoby wywołującej komendę."""

    explicit = str(supplied_username or "").strip()
    if explicit:
        return explicit

    try:
        profile = KOTONE_USERS_BY_DISCORD_ID.get(int(discord_user_id or 0))
    except (TypeError, ValueError):
        profile = None
    return str(profile.get("aoty_username") or "").strip() if profile else None


def is_operator_discord_id(discord_id: object) -> bool:
    """Sprawdź, czy konto Discord może uruchamiać komendy administracyjne."""

    try:
        return int(discord_id) in OPERATOR_DISCORD_IDS
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Sekrety i identyfikatory usług
# ---------------------------------------------------------------------------

LASTFM_API_KEY = str(os.getenv("LASTFM_API_KEY") or "").strip()
LASTFM_API_ENABLED = bool(LASTFM_API_KEY)
DISCOGS_TOKEN = str(os.getenv("DISCOGS_TOKEN") or "").strip()
DISCOGS_API_ENABLED = bool(DISCOGS_TOKEN)
PARSE_API_KEY = str(os.getenv("PARSE_API_KEY") or "").strip()
PARSE_API_ENABLED = bool(PARSE_API_KEY)

TOKEN = os.getenv("DISCORD_TOKEN") or CONFIG.get("discord_token", "")
if not TOKEN:
    raise RuntimeError(
        "Brak tokenu Discord. Ustaw DISCORD_TOKEN albo discord_token w config.json."
    )

APPLICATION_ID = int(CONFIG["application_id"])
GUILD_ID = int(CONFIG["guild_id"])
CHANNEL_ID = _DEFAULT_CHANNEL_ID
