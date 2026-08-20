"""Low-priority newest-to-oldest import of configured Kotone Last.fm users."""

from __future__ import annotations

import asyncio
import time

import lastfm
from database import DB
from lastfm_database import LASTFM_DB
from settings import (
    KOTONE_USERS,
    LASTFM_API_ENABLED,
    LASTFM_HISTORY_PAGE_INTERVAL,
    LASTFM_HISTORY_PAGE_SIZE,
    LASTFM_NEWEST_SCROBBLE_INTERVAL,
    LASTFM_PROFILE_SYNC_INTERVAL,
)
from source_switches import SOURCES


class LastFMArchive:
    """Import exactly one API page per call, rotating all Kotone profiles."""

    def __init__(self) -> None:
        self._cursor = 0
        self._next_run_at = 0.0
        self._in_progress = False
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
        """Run one background step without overlapping a manual import."""

        if self._in_progress:
            return {"attempted": False, "busy": True}
        self._in_progress = True
        try:
            return await self._run_one()
        finally:
            self._in_progress = False

    async def _run_one(self) -> dict:
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
            has_archive_cursor = state.get("total_pages") is not None
            newest_due = LASTFM_DB.newest_due(
                key, LASTFM_NEWEST_SCROBBLE_INTERVAL
            )
            # A CSV import marks its archive as complete.  Do not make an
            # expensive API crawl right afterwards: only a rare page-one
            # refresh is useful then.  This state is separate from scrobble
            # rows, so no per-row source column is required.
            if state.get("complete") and not newest_due:
                self.last_success_at = now
                self.last_error = None
                return {"attempted": True, "profile": refreshed_profile, "complete": True}
            if not has_archive_cursor or newest_due:
                # First request after startup always persists the latest page.
                # Later refreshes do not reset the historical cursor.
                page_number = 1
                refresh_only = has_archive_cursor
            else:
                page_number = max(1, int(state.get("next_page") or 1))
                refresh_only = False

            page = await asyncio.to_thread(
                lastfm.LASTFM.recent_tracks,
                username,
                page=page_number,
                limit=LASTFM_HISTORY_PAGE_SIZE,
            )
            if page is None:
                raise lastfm.LastFMUnavailable("Last.fm nie zwrócił historii scrobbli.")
            page["tracks"] = await asyncio.to_thread(
                DB.link_lastfm_tracks_to_releases,
                list(page.get("tracks") or []),
            )
            inserted = (
                LASTFM_DB.refresh_newest_page(key, page)
                if refresh_only
                else LASTFM_DB.import_page(key, page)
            )
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

    async def import_newest_now(
        self,
        profile_key: object,
        *,
        manual_override: bool = False,
    ) -> dict:
        """Manually seed one configured profile from its newest Last.fm page.

        The regular worker continues at page two later.  This is deliberately
        limited to one profile summary and one newest-first history page, so a
        slash command cannot turn into an unbounded API crawl.
        """

        key = str(profile_key or "").strip().casefold()
        profiles = dict(self._profiles())
        profile = profiles.get(key)
        if profile is None:
            return {"error": "Ten profil Kotone nie ma ustawionego konta Last.fm."}
        if not LASTFM_API_ENABLED:
            return {"error": "Brak LASTFM_API_KEY; Last.fm API jest wyłączone."}
        if not manual_override and not SOURCES.enabled("lastfm"):
            return {"error": "Last.fm API jest zablokowane w `/dbonly`."}
        if self._in_progress:
            return {"error": "Importer Last.fm pracuje już w tle; spróbuj za chwilę."}

        self._in_progress = True
        username = str(profile["lastfm_username"])
        now = time.time()
        try:
            summary = await asyncio.to_thread(lastfm.LASTFM.user_info, username)
            if summary is None:
                raise lastfm.LastFMUnavailable("Last.fm nie zwrócił profilu użytkownika.")
            LASTFM_DB.save_profile(key, summary)

            page = await asyncio.to_thread(
                lastfm.LASTFM.recent_tracks,
                username,
                page=1,
                limit=LASTFM_HISTORY_PAGE_SIZE,
            )
            if page is None:
                raise lastfm.LastFMUnavailable("Last.fm nie zwrócił historii scrobbli.")
            page["tracks"] = await asyncio.to_thread(
                DB.link_lastfm_tracks_to_releases,
                list(page.get("tracks") or []),
            )
            state = LASTFM_DB.state(key)
            if state.get("complete"):
                inserted = LASTFM_DB.refresh_newest_page(key, page)
            else:
                inserted = LASTFM_DB.import_page(key, page)
            self.last_success_at = now
            self.last_error = None
            return {
                "profile": summary,
                "inserted": inserted,
                "page": int(page.get("page") or 1),
                "total_pages": int(page.get("total_pages") or 1),
                "complete": bool(LASTFM_DB.state(key).get("complete")),
            }
        except lastfm.LastFMUnavailable as exc:
            LASTFM_DB.mark_error(key, exc)
            self.last_error = f"{type(exc).__name__}: {exc}"
            return {"error": self.last_error}
        except Exception as exc:
            LASTFM_DB.mark_error(key, exc)
            self.last_error = f"{type(exc).__name__}: {exc}"
            return {"error": self.last_error}
        finally:
            self._in_progress = False


LASTFM_ARCHIVE = LastFMArchive()
