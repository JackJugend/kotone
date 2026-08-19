"""Low-priority newest-to-oldest import of configured Kotone Last.fm users."""

from __future__ import annotations

import asyncio
import time

import lastfm
from lastfm_database import LASTFM_DB
from settings import (
    KOTONE_USERS,
    LASTFM_API_ENABLED,
    LASTFM_HISTORY_PAGE_INTERVAL,
    LASTFM_HISTORY_PAGE_SIZE,
    LASTFM_PROFILE_SYNC_INTERVAL,
)
from source_switches import SOURCES


class LastFMArchive:
    """Import exactly one API page per call, rotating all Kotone profiles."""

    def __init__(self) -> None:
        self._cursor = 0
        self._next_run_at = 0.0
        self.last_success_at: float | None = None
        self.last_error: str | None = None

    @staticmethod
    def _profiles() -> list[tuple[str, dict]]:
        return [
            (key, profile)
            for key, profile in KOTONE_USERS.items()
            if profile.get("lastfm_username")
        ]

    async def run_one(self) -> dict:
        """Fetch profile counters first, then one history page, oldest last."""

        now = time.time()
        if now < self._next_run_at:
            return {"attempted": False}
        if not LASTFM_API_ENABLED or not SOURCES.enabled("lastfm"):
            return {"attempted": False}
        profiles = self._profiles()
        if not profiles:
            return {"attempted": False}

        key, profile = profiles[self._cursor % len(profiles)]
        self._cursor = (self._cursor + 1) % len(profiles)
        username = str(profile["lastfm_username"])
        self._next_run_at = now + LASTFM_HISTORY_PAGE_INTERVAL
        try:
            # Do this before any long history crawl. It gives /profile the
            # useful total counters and ensures one fresh scrobble is imported
            # from page 1 before proceeding towards older pages.
            refreshed_profile = False
            if LASTFM_DB.profile_due(key, LASTFM_PROFILE_SYNC_INTERVAL):
                summary = await asyncio.to_thread(lastfm.LASTFM.user_info, username)
                if summary:
                    LASTFM_DB.save_profile(key, summary)
                    refreshed_profile = True

            state = LASTFM_DB.state(key)
            if state.get("complete"):
                if refreshed_profile:
                    page = await asyncio.to_thread(
                        lastfm.LASTFM.recent_tracks,
                        username,
                        page=1,
                        limit=LASTFM_HISTORY_PAGE_SIZE,
                    )
                    if page is None:
                        raise lastfm.LastFMUnavailable("Last.fm nie zwrócił nowych scrobbli.")
                    inserted = LASTFM_DB.refresh_newest_page(key, page)
                    self.last_success_at = now
                    self.last_error = None
                    print(f"[LASTFM ARCHIVE] {key}: odświeżono najnowsze scrobble (+{inserted}).")
                    return {"attempted": True, "profile": True, "inserted": inserted}
                self.last_success_at = now
                self.last_error = None
                return {"attempted": True, "profile": refreshed_profile, "complete": True}

            if refreshed_profile:
                self.last_success_at = now
                self.last_error = None
                print(f"[LASTFM ARCHIVE] {key}: zapisano podstawowe statystyki profilu.")
                # First pass is profile-only. The next rotation begins at page
                # one, so an empty archive never starts with a burst of calls.
                return {"attempted": True, "profile": True}

            page_number = max(1, int(state.get("next_page") or 1))
            page = await asyncio.to_thread(
                lastfm.LASTFM.recent_tracks,
                username,
                page=page_number,
                limit=LASTFM_HISTORY_PAGE_SIZE,
            )
            if page is None:
                raise lastfm.LastFMUnavailable("Last.fm nie zwrócił historii scrobbli.")
            inserted = LASTFM_DB.import_page(key, page)
            self.last_success_at = now
            self.last_error = None
            print(
                f"[LASTFM ARCHIVE] {key}: strona {page_number}/{page['total_pages']}, "
                f"dodano {inserted} scrobbli (najnowsze → najstarsze)."
            )
            return {"attempted": True, "profile": refreshed_profile, "inserted": inserted}
        except lastfm.LastFMUnavailable as exc:
            LASTFM_DB.mark_error(key, exc)
            self.last_error = f"{type(exc).__name__}: {exc}"
            return {"attempted": True, "error": self.last_error}
        except Exception as exc:
            LASTFM_DB.mark_error(key, exc)
            self.last_error = f"{type(exc).__name__}: {exc}"
            return {"attempted": True, "error": self.last_error}


LASTFM_ARCHIVE = LastFMArchive()
