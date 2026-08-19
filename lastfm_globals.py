"""One global, dependency-light access point for Kotone's Last.fm state.

Other modules should import shared Last.fm objects and configuration from here
instead of each reconstructing its own set of names.  The module performs no
network I/O at import time; it simply re-exports the single client/database/
archive instances used by the bot.
"""

from __future__ import annotations

from lastfm import LASTFM, LastFMClient, LastFMUnavailable
from lastfm_archive import LASTFM_ARCHIVE, LastFMArchive
from lastfm_database import LASTFM_DB, LastFMDatabase
from settings import (
    LASTFM_API_ENABLED,
    LASTFM_HISTORY_PAGE_INTERVAL,
    LASTFM_HISTORY_PAGE_SIZE,
    LASTFM_PROFILE_SYNC_INTERVAL,
)


__all__ = (
    "LASTFM",
    "LASTFM_ARCHIVE",
    "LASTFM_DB",
    "LASTFM_API_ENABLED",
    "LASTFM_HISTORY_PAGE_INTERVAL",
    "LASTFM_HISTORY_PAGE_SIZE",
    "LASTFM_PROFILE_SYNC_INTERVAL",
    "LastFMArchive",
    "LastFMClient",
    "LastFMDatabase",
    "LastFMUnavailable",
)

