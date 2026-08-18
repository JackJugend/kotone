"""Low-priority persistent cache/bootstrap worker.

This task is deliberately separate from the notification monitor. Its job is
to make SQLite increasingly self-sufficient without making /last, /artist or
new-rating notifications wait behind a large profile crawl.
"""

from __future__ import annotations

import asyncio
import time

import discord

from database import DB
from http_client import HTTP, PRIORITY_MAINTENANCE
from services import DATA
from settings import (
    ARCHIVE_WORKER_ERROR_SLEEP,
    ARCHIVE_WORKER_IDLE_SECONDS,
    ARCHIVE_WORKER_REST_SECONDS,
    ARCHIVE_WORKER_START_DELAY,
    DETAIL_ENRICH_PER_CYCLE,
    ENRICH_WORKER_REST_SECONDS,
    PROFILE_RATING_ARCHIVE_FORMATS_PER_CYCLE,
    RELEASE_ENRICH_PER_CYCLE,
    USERS,
)


class BackgroundWorker:
    """Bootstrap full configured-user archives, then maintain them forever."""

    def __init__(self, client: discord.Client):
        self.client = client
        self._stop_event = asyncio.Event()
        # Archive and enrichment are independent queues. Advancing one must
        # not accidentally skip a user in the other phase.
        self._cursor = 0
        self._enrich_cursor = 0
        self.last_run_at: float | None = None
        self.last_success_at: float | None = None
        self.last_error: str | None = None

    def stop(self) -> None:
        self._stop_event.set()

    async def _sleep(self, seconds: float) -> None:
        """Sleep interruptibly so Railway shutdown never waits for the timer."""
        try:
            await asyncio.wait_for(
                self._stop_event.wait(),
                timeout=max(0.0, float(seconds)),
            )
        except asyncio.TimeoutError:
            pass

    @staticmethod
    def _ordered_users_from(cursor: int) -> list[str]:
        """Return a round-robin view without advancing it speculatively."""

        if not USERS:
            return []

        start = int(cursor) % len(USERS)
        return USERS[start:] + USERS[:start]

    @staticmethod
    def _cursor_after(username: str) -> int:
        """Return the slot after the user that consumed a real work unit."""

        if not USERS:
            return 0
        try:
            position = USERS.index(username)
        except ValueError:
            return 0
        return (position + 1) % len(USERS)

    async def _enrich_one_user(self, username: str) -> dict:
        return await DATA.enrich_user(
            username,
            detail_limit=DETAIL_ENRICH_PER_CYCLE,
            release_limit=RELEASE_ENRICH_PER_CYCLE,
            priority=PRIORITY_MAINTENANCE,
        )

    async def _run_once(self) -> float:
        """Do one bounded unit of maintenance and return the next sleep time."""
        self.last_run_at = time.time()

        if HTTP.db_only_enabled():
            self.last_success_at = self.last_run_at
            self.last_error = "Tryb /dbonly aktywny; maintenance AOTY pominięty."
            return ARCHIVE_WORKER_IDLE_SECONDS

        # Phase 1: ratings first. Exactly one configured user's bounded format
        # batch per pass keeps pressure predictable, while the short rest means
        # first bootstrap no longer waits 20 minutes for every next format.
        for username in self._ordered_users_from(self._cursor):
            try:
                result = await DATA.archive_profile_ratings(
                    username,
                    formats_per_cycle=(
                        PROFILE_RATING_ARCHIVE_FORMATS_PER_CYCLE
                    ),
                    priority=PRIORITY_MAINTENANCE,
                )
            except Exception:
                # A broken user/route must not monopolize the first position
                # forever. CancelledError remains untouched on modern Python.
                self._cursor = self._cursor_after(username)
                raise

            if not result.get("formats_attempted", 0):
                continue

            self._cursor = self._cursor_after(username)

            if result.get("errors"):
                self.last_error = str(
                    result.get("last_error") or "archive error"
                )
                return ARCHIVE_WORKER_ERROR_SLEEP

            self.last_success_at = time.time()
            self.last_error = None
            return ARCHIVE_WORKER_REST_SECONDS

        # Phase 2: every format is complete/not due. Fill public album data,
        # reviews and Track Ratings a few rows at a time. This may take longer
        # than the initial rating bootstrap, but never blocks the monitor.
        for username in self._ordered_users_from(self._enrich_cursor):
            try:
                result = await self._enrich_one_user(username)
            except Exception:
                self._enrich_cursor = self._cursor_after(username)
                raise

            if result.get("errors"):
                self._enrich_cursor = self._cursor_after(username)
                self.last_error = "enrichment error"
                return ARCHIVE_WORKER_ERROR_SLEEP

            if result.get("releases") or result.get("details"):
                self._enrich_cursor = self._cursor_after(username)
                self.last_success_at = time.time()
                self.last_error = None
                return ENRICH_WORKER_REST_SECONDS

        # Nothing to do right now. Daily format maintenance will make rows due
        # again later; sleeping avoids a useless SQLite busy loop.
        self.last_error = None
        return ARCHIVE_WORKER_IDLE_SECONDS

    async def run(self) -> None:
        await self.client.wait_until_ready()
        await self._sleep(ARCHIVE_WORKER_START_DELAY)

        print(
            "[BACKGROUND] Start: pełne profile → SQLite, "
            "najniższy priorytet HTTP."
        )

        while not self.client.is_closed() and not self._stop_event.is_set():
            try:
                sleep_for = await self._run_once()
                DB.backup_if_due()
            except Exception as exc:
                # Background cache is useful, but never important enough to
                # kill the long-running bot. A future pass retries safely.
                self.last_error = f"{type(exc).__name__}: {exc}"
                sleep_for = ARCHIVE_WORKER_ERROR_SLEEP
                print(
                    f"[BACKGROUND] Nieobsłużony błąd: {self.last_error}; "
                    f"pauza {int(sleep_for)} s."
                )

            await self._sleep(sleep_for)
