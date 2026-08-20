"""Globalne, ręcznie zweryfikowane aliasy nazw artystów."""

from __future__ import annotations


ARTIST_ALIAS_GROUPS = (
    ("Глюкі", "Hluki", "Hliuki", "Gluki"),
    ("LOONA", "이달의 소녀", "이달의 소녀 [LOONA]"),
)


def artist_alias_variants(value: object) -> tuple[str, ...]:
    """Zwróć znane pisownie zapytania razem z oryginalną wartością."""

    text = " ".join(str(value or "").split())
    if not text:
        return ()

    key = text.casefold()
    for group in ARTIST_ALIAS_GROUPS:
        normalized_group = {" ".join(name.split()).casefold() for name in group}
        if key in normalized_group:
            return tuple(dict.fromkeys((text, *group)))
    return (text,)

