"""Centralna konfiguracja Kotone.

Ten moduł jest jedynym miejscem, które czyta config.json i wylicza ścieżki.
Dzięki temu każda komenda korzysta z dokładnie tych samych ustawień,
formatów i assetów.
"""

from __future__ import annotations

import json
import os
import shutil

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
BASE_URL = "https://www.albumoftheyear.org"

# Asset używany w footerach embedów.
AOTY_ICON = os.path.join(BASE_DIR, "assets", "aoty.jpg")
AOTY_ICON_FILENAME = "aoty.jpg"
AOTY_ICON_ATTACHMENT = "attachment://aoty.jpg"

DEFAULT_DATA_FILE = os.path.join(BASE_DIR, "data.json")
DATA_DIR = os.getenv("DATA_DIR")

if DATA_DIR:
    os.makedirs(DATA_DIR, exist_ok=True)
    DATA_FILE = os.path.join(DATA_DIR, "data.json")

    # Przy pierwszym uruchomieniu wolumenu kopiujemy stan z projektu.
    if not os.path.exists(DATA_FILE) and os.path.exists(DEFAULT_DATA_FILE):
        shutil.copyfile(DEFAULT_DATA_FILE, DATA_FILE)
else:
    DATA_FILE = DEFAULT_DATA_FILE

with open(CONFIG_FILE, "r", encoding="utf-8") as file:
    CONFIG = json.load(file)

TOKEN = os.getenv("DISCORD_TOKEN") or CONFIG.get("discord_token", "")

if not TOKEN:
    raise RuntimeError(
        "Brak tokenu Discord. Ustaw DISCORD_TOKEN albo discord_token w config.json."
    )

APPLICATION_ID = int(CONFIG["application_id"])
GUILD_ID = int(CONFIG["guild_id"])
CHANNEL_ID = int(CONFIG["channel_id"])
USERS = [str(user).strip() for user in CONFIG.get("users", []) if str(user).strip()]

USER_CHANNELS = {
    str(username).casefold(): int(channel_id)
    for username, channel_id in CONFIG.get("user_channels", {}).items()
}

CHECK_INTERVAL = max(60, int(CONFIG.get("check_interval", 300)))

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
