"""Jeden katalog formatów AOTY używany w całym bocie.

Ten moduł nie czyta ``config.json`` i nie zależy od Discorda ani SQLite.
Zawiera wyłącznie stabilne znaczenie formatów: klucz wewnętrzny, slug AOTY
i etykietę wyświetlaną użytkownikowi.  Dzięki temu parser, baza, komendy
i statystyki nie utrzymują własnych, rozjeżdżających się list formatów.
"""

from __future__ import annotations

import re


# Kolejność jest celowa: zachowuje kolejność menu Discorda i archiwum AOTY.
RATING_FORMATS: dict[str, dict[str, str]] = {
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


# Domyślne limity szybkiego monitora.  Zero oznacza, że format zachowuje się
# jak pełne archiwum w tle, ale nie jest pobierany w każdej szybkiej rundzie.
DEFAULT_RATING_FETCH_LIMITS: dict[str, int] = {
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


def build_rating_fetch_limits(raw_limits: object) -> dict[str, int]:
    """Zbuduj bezpieczne limity z configu dla każdego znanego formatu.

    W configu można użyć klucza Kotone (np. ``music_video``) albo sluga AOTY
    (``music-video``). Nieznane wpisy są celowo ignorowane: nie mogą dodać
    parserowi nieobsługiwanej ścieżki.
    """

    configured = raw_limits if isinstance(raw_limits, dict) else {}
    limits: dict[str, int] = {}

    for key, info in RATING_FORMATS.items():
        default = DEFAULT_RATING_FETCH_LIMITS.get(key, 0)
        raw_value = configured.get(key, configured.get(info["slug"], default))
        try:
            limits[key] = max(0, int(raw_value))
        except (TypeError, ValueError):
            limits[key] = default

    return limits


def format_key_from_label(value: object) -> str | None:
    """Zamień etykietę widoczną na AOTY/Discordzie na klucz Kotone."""

    needle = re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())
    if not needle:
        return None
    for key, info in RATING_FORMATS.items():
        candidates = {
            re.sub(r"[^a-z0-9]+", "", key.casefold()),
            re.sub(r"[^a-z0-9]+", "", info["slug"].casefold()),
            re.sub(r"[^a-z0-9]+", "", info["label"].casefold()),
        }
        if needle in candidates:
            return key
    return None
