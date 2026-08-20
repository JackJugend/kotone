"""Globalne assety, emoji i symbole interfejsu Kotone.

W kodzie komend nie powinny występować skopiowane ID emoji ani osobne nazwy
buttonów. Wszystkie stabilne elementy prezentacji są zebrane w tym pliku.
Dynamiczne ID emoji (score, avatar i źródło manualne) nadal żyją w SQLite.
"""

from __future__ import annotations

import os

from config_core import BASE_DIR


# ---------------------------------------------------------------------------
# Assety embedów
# ---------------------------------------------------------------------------

AOTY_ICON = os.path.join(BASE_DIR, "assets", "aoty.jpg")
AOTY_ICON_FILENAME = "aoty.jpg"
AOTY_ICON_URL = "https://cdn.albumoftheyear.org/images/favicon.png"
AOTY_ICON_ATTACHMENT = AOTY_ICON_URL

LASTFM_ICON = os.path.join(BASE_DIR, "assets", "lastfm.png")
LASTFM_ICON_FILENAME = "lastfm.png"
LASTFM_ICON_ATTACHMENT = f"attachment://{LASTFM_ICON_FILENAME}"


# ---------------------------------------------------------------------------
# Symbole głównych zakładek
# ---------------------------------------------------------------------------

ARTIST_BUTTON = "★"
DETAILS_BUTTON = "🛈"
HOME_BUTTON = "🏠︎"
TRACKLIST_BUTTON = "☰"
REVIEW_BUTTON = "✎"

ACTION_TABS = {
    "artist": ARTIST_BUTTON,
    "details": DETAILS_BUTTON,
    "home": HOME_BUTTON,
    "tracklist": TRACKLIST_BUTTON,
    "review": REVIEW_BUTTON,
}
ACTION_BUTTON_ORDER = tuple(ACTION_TABS.values())
VIEW_TIMEOUT_SECONDS = 20 * 60


# ---------------------------------------------------------------------------
# Emoji źródeł i Must Hear
# ---------------------------------------------------------------------------

AOTY_SOURCE_EMOJI = "<:aoty:1539095897084924004>"
MUSICBRAINZ_SOURCE_EMOJI = "<:music_brainz:1539096206083629186>"
LASTFM_SOURCE_EMOJI = "<:lastfm:1539689853506293760>"
MANUAL_SOURCE_EMOJI = ""

MUST_HEAR_USERS_EMOJI = "<:musthear_users:1539713390820458566>"
MUST_HEAR_CRITICS_EMOJI = "<:musthear_critics:1539713389557841981>"
MUST_HEAR_BOTH_EMOJI = "<:musthear_both:1539713387150319679>"

SOURCE_EMOJIS = {
    "aoty": AOTY_SOURCE_EMOJI,
    "musicbrainz": MUSICBRAINZ_SOURCE_EMOJI,
    "lastfm": LASTFM_SOURCE_EMOJI,
    "manual": MANUAL_SOURCE_EMOJI,
}
MUST_HEAR_EMOJIS = {
    "users": MUST_HEAR_USERS_EMOJI,
    "critics": MUST_HEAR_CRITICS_EMOJI,
    "both": MUST_HEAR_BOTH_EMOJI,
}


# ---------------------------------------------------------------------------
# Emoji ocen i flag pozycji
# ---------------------------------------------------------------------------

# Discord nie renderuje application emoji w opisach opcji Select. Dlatego
# embedy i dropdowny świadomie mają osobne, jednoznacznie nazwane warianty.
EMBED_STATUS_EMOJIS = {
    "review": "\\✎",
    "tracklist": "\\☰",
    "like": "\\❤︎⁠",
}
MENU_STATUS_EMOJIS = {
    "review": "✎",
    "tracklist": "☰",
    "like": "❤︎⁠",
}

EMBED_SCORE_EMOJIS = {
    "unrated": "\\⚪",
    "diamond": "\\💎",
    "green": "\\💚",
    "lime": "\\🟢",
    "yellow": "\\🟡",
    "orange": "\\🟠",
    "red": "\\🔴",
    "unknown": "\\❓",
    "black": "\\⚫",
}
MENU_SCORE_EMOJIS = {
    key: value.removeprefix("\\")
    for key, value in EMBED_SCORE_EMOJIS.items()
}

# Te emoji zostały przygotowane ręcznie i należą do aplikacji Kotone.
MANUAL_STATUS_EMOJI_IDS = {
    "tracklist": "1539780590751187014",
    "review": "1539780589240983592",
    "like": "1539780587936551002",
}
APPLICATION_STATUS_EMOJIS = {
    key: f"<:kotone_{key}:{emoji_id}>"
    for key, emoji_id in MANUAL_STATUS_EMOJI_IDS.items()
}

