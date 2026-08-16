import re
import unicodedata


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
