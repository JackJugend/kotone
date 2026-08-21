"""Wspólne formatowanie nazw, gatunków i dat wyświetlanych przez Kotone."""

import re
import unicodedata
from datetime import datetime


_EDITION_WORDS = {
    "anniversary",
    "bonus",
    "deluxe",
    "demo",
    "edition",
    "expanded",
    "instrumental",
    "live",
    "mix",
    "mono",
    "ost",
    "original soundtrack",
    "remaster",
    "remastered",
    "remix",
    "reissue",
    "single",
    "soundtrack",
    "stereo",
    "version",
}

_GENRE_WORD_FORMS = {
    "r&b": "R&B",
    "edm": "EDM",
    "idm": "IDM",
    "k-pop": "K-Pop",
    "j-pop": "J-Pop",
    "c-pop": "C-Pop",
    "hip-hop": "Hip-Hop",
    "nu-metal": "Nu Metal",
    # Utrwalony wariant AOTY/MB bywa zapisywany z łącznikiem albo bez niego.
    # Kotone pokazuje i przechowuje zawsze jedną, krótszą formę.
    "synthpop": "Synthpop",
    "synth-pop": "Synthpop",
}


def normalize_genres(values) -> list[str]:
    """Zwróć kanoniczne, unikalne nazwy gatunków.

    To jest jeden punkt normalizacji dla całego bota: ``ambient``,
    ``Ambient`` i ``AMBIENT`` stają się jedną pozycją ``Ambient``.  Funkcja
    nie próbuje łączyć różnych gatunków znaczeniowo — porządkuje wyłącznie
    wielkość liter, spacje, równoważne znaki łącznika oraz kilka znanych
    wariantów pisowni (np. ``Synth-pop`` → ``Synthpop``).
    """

    result: list[str] = []
    seen: set[str] = set()
    for raw in values or []:
        text = re.sub(r"[‐‑‒–—−]", "-", str(raw or ""))
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        label = _display_genre_label(text)
        key = label.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(label)
    return result


def display_genres(values) -> list[str]:
    """Zwróć kanoniczne gatunki gotowe do wyświetlenia w Discordzie."""

    return normalize_genres(values)


def _display_genre_label(text: str) -> str:
    known = _GENRE_WORD_FORMS.get(text.casefold())
    if known:
        return known

    def format_part(part: str) -> str:
        folded = part.casefold()
        if folded in _GENRE_WORD_FORMS:
            return _GENRE_WORD_FORMS[folded]
        if part.isupper() and len(part) <= 4:
            return part
        return part[:1].upper() + part[1:].lower()

    words = []
    for word in text.split(" "):
        words.append("-".join(format_part(part) for part in word.split("-")))
    return " ".join(words)


def display_release_date(value, *, missing: str = "—") -> str:
    """Formatuj znaną datę wydania jednolicie jako ``DD.MM.RRRR``."""

    text = str(value or "").strip()
    if not text or text == missing:
        return missing
    if re.fullmatch(r"\d{4}", text):
        return text

    parsers = (
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%d.%m.%Y",
        "%B %d, %Y",
        "%b %d, %Y",
        "%d %B %Y",
        "%d %b %Y",
    )
    parsed = None
    for parser in parsers:
        try:
            parsed = datetime.strptime(text, parser)
            break
        except ValueError:
            continue
    if parsed is None:
        return text
    return parsed.strftime("%d.%m.%Y")


def _letter_script_stats(text):
    letters = 0
    latin = 0
    non_latin = 0

    for char in str(text or ""):
        if not char.isalpha():
            continue

        letters += 1
        unicode_name = unicodedata.name(char, "")

        if "LATIN" in unicode_name:
            latin += 1
        else:
            non_latin += 1

    return letters, latin, non_latin


def _looks_like_edition_tag(text):
    normalized = " ".join(
        re.sub(
            r"[^a-z0-9]+",
            " ",
            str(text or "").casefold(),
        ).split()
    )

    if not normalized:
        return True

    if normalized in _EDITION_WORDS:
        return True

    tokens = normalized.split()

    if any(word in tokens for word in {
        "edition",
        "version",
        "remaster",
        "remastered",
        "deluxe",
        "anniversary",
        "bonus",
        "soundtrack",
        "ost",
        "reissue",
        "mix",
        "remix",
        "live",
        "demo",
        "single",
    }):
        return True

    return False


def display_romanized_name(text):
    """
    Do WYŚWIETLANIA nazw z AOTY.

    Przykłady:
        椎名林檎 [Ringo Sheena] -> Ringo Sheena
        死んだ僕の彼女 [my dead girlfriend] -> my dead girlfriend
        [X X] -> [X X]
        Album [Deluxe] -> Album [Deluxe]

    Oryginalnej nazwy nie należy zastępować tą wartością przy
    wyszukiwaniu, fuzzy matchingu ani identyfikacji albumu/artysty.
    """
    if not text:
        return text

    text = str(text).strip()

    match = re.fullmatch(
        r"(?s)(.+?)\s*\[([^\[\]]+)\]\s*",
        text,
    )

    if not match:
        return text

    original = match.group(1).strip()
    candidate = match.group(2).strip()

    if not original or not candidate:
        return text

    original_letters, _, original_non_latin = _letter_script_stats(
        original
    )
    candidate_letters, candidate_latin, _ = _letter_script_stats(
        candidate
    )

    if original_letters == 0 or candidate_letters < 2:
        return text

    # Oryginalna część musi być w większości nielacińska.
    if original_non_latin == 0:
        return text

    non_latin_ratio = original_non_latin / original_letters

    if non_latin_ratio < 0.50:
        return text

    # Zawartość [] musi wyglądać jak romanizacja.
    latin_ratio = candidate_latin / candidate_letters

    if latin_ratio < 0.70:
        return text

    # Nie traktujemy typowych dopisków edycji jako romanizacji.
    if _looks_like_edition_tag(candidate):
        return text

    return candidate
"""Wspólne formatowanie nazw, gatunków i dat wyświetlanych przez Kotone."""
