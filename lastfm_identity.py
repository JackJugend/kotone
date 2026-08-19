"""Stable, conservative identities for Last.fm scrobbles.

Names remain exactly as Last.fm supplied them for display.  These keys are
used only for deduplication and counters, so alternate scripts and explicit
romanisations can share a single artist/album bucket without rewriting the
original listening history.
"""

from __future__ import annotations

from rating_import import normalized_text


_ARTIST_ALIASES = {
    normalized_text(alias): normalized_text("Sheena Ringo")
    for alias in (
        "椎名林檎",
        "椎名林檎 [Ringo Sheena]",
        "Ringo Sheena",
        "Sheena Ringo",
    )
}

_SHEENA_RINGO_ALBUMS = {
    normalized_text(alias): normalized_text("Kalk Samen Chestnut Flower")
    for alias in (
        "加爾基 精液 栗ノ花",
        "Kalk Samen Chestnut Flower",
        "加爾基 精液 栗ノ花 Kalk Samen Chestnut Flower",
        "加爾基 精液 栗ノ花 [Kalk Samen Chestnut Flower]",
    )
}


def artist_key(name: object, mbid: object = None) -> str:
    """Return a non-display artist identity; known aliases beat a raw MBID."""

    normalized = normalized_text(name)
    canonical = _ARTIST_ALIASES.get(normalized)
    if canonical:
        return f"name:{canonical}"
    identifier = str(mbid or "").strip().casefold()
    return f"mbid:{identifier}" if identifier else f"name:{normalized}"


def album_key(
    artist: object,
    album: object,
    *,
    artist_mbid: object = None,
    album_mbid: object = None,
) -> str:
    """Return an album identity scoped to its canonical artist identity."""

    artist_identity = artist_key(artist, artist_mbid)
    normalized_album = normalized_text(album)
    if artist_identity == f"name:{normalized_text('Sheena Ringo')}":
        canonical = _SHEENA_RINGO_ALBUMS.get(normalized_album)
        if canonical:
            return f"{artist_identity}|name:{canonical}"
    identifier = str(album_mbid or "").strip().casefold()
    if identifier:
        return f"mbid:{identifier}"
    return f"{artist_identity}|name:{normalized_album}"


def track_key(name: object, mbid: object = None) -> str:
    identifier = str(mbid or "").strip().casefold()
    return f"mbid:{identifier}" if identifier else f"name:{normalized_text(name)}"
