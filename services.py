"""Application service layer.

Discord commands should ask this module for data instead of deciding themselves
whether to hit AOTY or SQLite.  That gives every command the same behavior:

1. use fresh SQLite data for configured users when possible;
2. refresh from AOTY when data is stale/missing;
3. persist only configured users;
4. fall back to stale SQLite data when AOTY is down/rate-limited;
5. arbitrary non-config users remain live-only and are never persisted.
"""

from __future__ import annotations

import asyncio
import time

import requests

import aoty
from database import DB
from http_client import (
    PRIORITY_BACKGROUND,
    PRIORITY_INTERACTIVE,
    PRIORITY_MAINTENANCE,
    PRIORITY_NORMAL,
    call_with_priority,
)
from settings import (
    CHECK_INTERVAL,
    FULL_SYNC_INTERVAL,
    PROFILE_SYNC_INTERVAL,
    PROFILE_RATING_ARCHIVE_FORMATS_PER_CYCLE,
    PROFILE_RATING_ARCHIVE_INTERVAL,
    PROFILE_RATING_ARCHIVE_LIMIT_PER_FORMAT,
    QUICK_RATING_LIMIT_PER_FORMAT,
    RATING_DETAIL_TTL,
    RATING_FETCH_LIMITS,
    RATING_FORMATS,
    RELEASE_DETAIL_TTL,
)


async def _thread_call(priority: int, func, /, *args, **kwargs):
    """Run synchronous scraper code without blocking Discord's event loop."""

    return await asyncio.to_thread(
        call_with_priority,
        priority,
        func,
        *args,
        **kwargs,
    )


class DataService:
    def __init__(self):
        # A single malformed release/user-detail page must not permanently
        # block enrichment of every later album. Failed items get a temporary
        # in-memory cooldown; a process restart safely clears it.
        self._detail_retry_after: dict[tuple[str, str], float] = {}
        self._release_retry_after: dict[str, float] = {}

    def is_monitored(self, username: str) -> bool:
        return DB.is_monitored(username)

    @staticmethod
    def _age(timestamp: float | None) -> float:
        if not timestamp:
            return float("inf")
        return max(0.0, time.time() - float(timestamp))

    # ------------------------------------------------------------------
    # User existence / profile
    # ------------------------------------------------------------------

    async def user_exists(self, username: str) -> bool:
        username = str(username or "").strip()
        if not username:
            return False

        # A configured user is part of Kotone's authoritative local scope.
        # Commands should keep working from SQLite even during a total AOTY
        # outage, so do not perform an existence request for config users.
        if DB.is_monitored(username):
            return True

        return await _thread_call(
            PRIORITY_INTERACTIVE,
            aoty.aoty_user_exists,
            username,
        )

    async def sync_profile(self, username: str, *, priority: int = PRIORITY_BACKGROUND) -> dict:
        """Refresh profile summary for one configured user."""

        if not DB.is_monitored(username):
            raise ValueError("sync_profile zapisuje tylko użytkowników z config")

        try:
            profile = await _thread_call(
                priority,
                aoty.get_profile_summary,
                username,
            )
            DB.save_profile(username, profile)
            return profile
        except Exception as exc:
            DB.mark_sync_error(username, f"profile: {type(exc).__name__}: {exc}")
            raise

    async def get_profile(self, username: str, *, recent_limit: int = 50) -> dict:
        """Profile command data with SQLite stale fallback for config users."""

        username = str(username).strip()

        if not DB.is_monitored(username):
            # Privacy/scope rule: arbitrary users are never persisted.
            return await _thread_call(
                PRIORITY_INTERACTIVE,
                aoty.get_profile_data,
                username,
                recent_limit,
            )

        cached = DB.get_profile(username, recent_limit=recent_limit)
        timestamps = DB.sync_timestamps(username)
        fresh = self._age(timestamps.get("profile_synced_at")) <= PROFILE_SYNC_INTERVAL

        if cached is not None and fresh:
            return cached

        try:
            await self.sync_profile(username, priority=PRIORITY_INTERACTIVE)
            refreshed = DB.get_profile(username, recent_limit=recent_limit)
            if refreshed is not None:
                return refreshed
        except (aoty.AOTYRateLimit, requests.RequestException):
            if cached is not None:
                return cached
            raise

        if cached is not None:
            return cached
        raise aoty.AOTYUserNotFound()

    async def get_avatar(self, username: str) -> str | None:
        if DB.is_monitored(username):
            cached = DB.get_avatar(username)
            if cached:
                return cached
            try:
                await self.sync_profile(username, priority=PRIORITY_INTERACTIVE)
                return DB.get_avatar(username)
            except Exception:
                return None

        try:
            return await _thread_call(
                PRIORITY_INTERACTIVE,
                aoty.get_user_avatar,
                username,
            )
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Ratings
    # ------------------------------------------------------------------

    @staticmethod
    def quick_fetch_limits() -> dict[str, int]:
        limits = {}
        for key, configured in RATING_FETCH_LIMITS.items():
            configured = max(0, int(configured))
            limits[key] = (
                min(configured, QUICK_RATING_LIMIT_PER_FORMAT)
                if configured > 0
                else 0
            )
        return limits

    async def fetch_ratings_live(
        self,
        username: str,
        *,
        full: bool,
        priority: int = PRIORITY_BACKGROUND,
    ) -> list[dict]:
        """Fetch monitor input with two very different cost profiles.

        Quick cycles use AOTY's combined recent route (+ Single/Music Video),
        so a normal 20-minute check is roughly three requests instead of one
        request for every enabled format. Full cycles still use explicit
        format routes because they are responsible for detecting edits to old
        ratings outside the recent window.
        """
        if full:
            return await _thread_call(
                priority,
                aoty.get_ratings,
                username,
                None,
                RATING_FETCH_LIMITS,
            )

        recent_count = min(
            50,
            max(20, QUICK_RATING_LIMIT_PER_FORMAT * 2),
        )
        recent = await _thread_call(
            priority,
            aoty.get_recent_ratings,
            username,
            recent_count,
            "all",
        )

        enabled_labels = {
            str(RATING_FORMATS[key]["label"]).casefold()
            for key, limit in RATING_FETCH_LIMITS.items()
            if int(limit or 0) > 0 and key in RATING_FORMATS
        }

        # Recent ratings in notification-disabled formats are still part of
        # the configured user's profile. Save those silently so /last all and
        # /profile stay useful without waiting for the slower archive sweep.
        # Enabled formats are deliberately NOT saved here: the monitor must
        # compare their old/new scores first so a change notification cannot be
        # swallowed by the cache layer.
        if DB.is_monitored(username):
            silent_items = [
                item
                for item in recent
                if item.get("release_format")
                and str(item.get("release_format")).casefold() not in enabled_labels
            ]
            if silent_items:
                DB.upsert_ratings(
                    username,
                    silent_items,
                    record_history=False,
                )

        if not enabled_labels:
            return []

        # Cards from the combined route normally contain their exact format.
        # If AOTY temporarily omits the label, keep the item rather than miss a
        # genuine new rating; the next full sync will normalize its format.
        return [
            item
            for item in recent
            if not item.get("release_format")
            or str(item.get("release_format")).casefold() in enabled_labels
        ]

    async def get_recent_ratings(
        self,
        username: str,
        count: int = 20,
        format_key: str = "all",
    ) -> list[dict]:
        username = str(username).strip()
        count = max(1, min(50, int(count)))

        if not DB.is_monitored(username):
            return await _thread_call(
                PRIORITY_INTERACTIVE,
                aoty.get_recent_ratings,
                username,
                count,
                format_key,
            )

        format_label = None
        if format_key and format_key != "all":
            format_label = RATING_FORMATS.get(format_key, {}).get("label")

        cached = DB.get_recent_ratings(
            username,
            count,
            release_format=format_label,
        )
        timestamps = DB.sync_timestamps(username)
        # The monitor is the primary live updater. Commands normally hit only
        # SQLite and therefore do not multiply AOTY traffic.
        fresh = self._age(timestamps.get("ratings_synced_at")) <= max(
            CHECK_INTERVAL * 1.5,
            5 * 60,
        )

        # A format disabled for notifications has its own slower archive
        # freshness. Before that archive has reached the format, an explicit
        # /last <format> request is allowed to fetch it live once instead of
        # incorrectly reporting an empty result just because some *other*
        # monitored formats were synced recently.
        if format_key and format_key != "all":
            configured_limit = int(RATING_FETCH_LIMITS.get(format_key, 0) or 0)
            if configured_limit <= 0:
                archive_row = DB.archive_status(username).get(format_key, {})
                archive_fresh = (
                    self._age(archive_row.get("last_success_at"))
                    <= PROFILE_RATING_ARCHIVE_INTERVAL
                )
                fresh = archive_fresh

        if fresh:
            return cached

        try:
            live = await _thread_call(
                PRIORITY_INTERACTIVE,
                aoty.get_recent_ratings,
                username,
                max(count, 10),
                format_key,
            )
            # Partial command refresh: never mark other DB ratings inactive.
            DB.upsert_ratings(username, live, record_history=False)
            return live[:count]
        except (aoty.AOTYRateLimit, requests.RequestException):
            if cached:
                return cached
            raise

    def cached_rating(self, username: str, album_id: str) -> dict | None:
        return DB.get_rating(username, album_id) if DB.is_monitored(username) else None

    async def get_user_rating_for_album(
        self,
        username: str,
        album_id: str,
        album_url: str | None,
        release_format: str | None,
        *,
        fallback_limit: int | None = 60,
        user_release_url: str | None = None,
        album_title: str | None = None,
        require_detail: bool = True,
    ) -> dict:
        """One user's rating for one album, cached persistently only for config users."""

        monitored = DB.is_monitored(username)
        if monitored:
            cached = (
                DB.get_rating_detail(username, album_id)
                if require_detail
                else DB.get_rating(username, album_id)
            )
        else:
            cached = None

        if cached is not None:
            if not require_detail:
                return cached
            if (
                cached.get("detail_complete")
                and self._age(cached.get("detail_synced_at")) <= RATING_DETAIL_TTL
            ):
                return cached

        try:
            live = await _thread_call(
                PRIORITY_INTERACTIVE,
                aoty.get_user_rating_for_album,
                username,
                album_id,
                album_url,
                release_format,
                fallback_limit,
                user_release_url,
                album_title,
            )

            if monitored:
                # If /album asks for a rating not yet present locally, only save
                # it when it is real. NR is not persisted as a fake rating row.
                if live.get("score") is not None and DB.get_rating(username, album_id) is None:
                    seed = {
                        "album_id": album_id,
                        "score": live.get("score"),
                        "date": live.get("date"),
                        "album": album_title,
                        "url": album_url,
                        "release_format": release_format,
                        "has_review": live.get("has_review"),
                        "has_track_ratings": live.get("has_track_ratings"),
                        "liked": live.get("liked"),
                        "review_url": live.get("review_url"),
                    }
                    DB.upsert_rating(username, seed, record_history=False)

                if DB.get_rating(username, album_id) is not None:
                    DB.save_rating_detail(username, album_id, live)

            return live

        except (aoty.AOTYRateLimit, requests.RequestException):
            if cached is not None:
                return cached
            raise

    # ------------------------------------------------------------------
    # Public release details
    # ------------------------------------------------------------------

    async def get_release_details(
        self,
        item: dict,
        *,
        username: str | None = None,
    ) -> dict:
        item = dict(item or {})
        album_id = str(item.get("album_id") or "")
        url = item.get("url")
        # Public release cache is safe to reuse from every command. Database
        # scope enforcement still guarantees that a release is persisted only
        # when at least one configured user has rated it.
        cached = DB.get_release_details(album_id) if album_id else None
        if cached is not None and self._age(cached.get("fetched_at")) <= RELEASE_DETAIL_TTL:
            return cached

        if not url:
            return cached or {}

        try:
            details = await _thread_call(
                PRIORITY_NORMAL,
                aoty.get_album_details,
                url,
            )
            if album_id:
                # save_release_details() itself checks whether the album is in
                # the configured-user scope. Public searches are never enough
                # to create a persistent row on their own.
                DB.save_release_details(album_id, details)
            return details
        except (aoty.AOTYRateLimit, requests.RequestException):
            if cached is not None:
                return cached
            raise

    # ------------------------------------------------------------------
    # Silent full-profile rating archive
    # ------------------------------------------------------------------

    async def archive_profile_ratings(
        self,
        username: str,
        *,
        formats_per_cycle: int | None = None,
        priority: int = PRIORITY_MAINTENANCE,
    ) -> dict:
        """Archive all AOTY rating formats for a configured user.

        First-time bootstrap is driven by a dedicated worker, not by the
        20-minute notification monitor. The worker asks for one format at a
        time and immediately continues after a short rest, so SQLite fills
        steadily while all HTTP requests still pass through the global
        low-priority rate limiter.

        ``profile_rating_archive_limit_per_format = 0`` means unlimited.
        Notification-enabled formats preserve already-known scores so this
        background job cannot consume a score change before the monitor sends
        its Discord notification. Older/missing rows are still added.
        """
        canonical = DB.canonical_username(username)
        if canonical is None:
            return {
                "formats_due": 0,
                "formats_attempted": 0,
                "formats_ok": 0,
                "ratings": 0,
                "errors": 0,
            }

        if formats_per_cycle is None:
            formats_per_cycle = PROFILE_RATING_ARCHIVE_FORMATS_PER_CYCLE

        due = DB.archive_due_formats(
            canonical,
            RATING_FORMATS.keys(),
            interval=PROFILE_RATING_ARCHIVE_INTERVAL,
            limit=max(0, int(formats_per_cycle)),
        )

        saved = 0
        errors = 0
        attempted = 0
        succeeded = 0
        last_format = None
        last_error = None

        for format_key in due:
            attempted += 1
            last_format = format_key
            info = RATING_FORMATS[format_key]

            try:
                if PROFILE_RATING_ARCHIVE_LIMIT_PER_FORMAT <= 0:
                    ratings = await _thread_call(
                        priority,
                        aoty.get_all_ratings_for_format,
                        canonical,
                        format_key,
                    )
                else:
                    ratings = await _thread_call(
                        priority,
                        aoty.get_ratings_for_format,
                        canonical,
                        format_key,
                        PROFILE_RATING_ARCHIVE_LIMIT_PER_FORMAT,
                    )

                notification_enabled = int(
                    RATING_FETCH_LIMITS.get(format_key, 0) or 0
                ) > 0
                previous_archive = DB.archive_status(canonical).get(
                    format_key,
                    {},
                )
                was_bootstrapped = bool(
                    previous_archive.get("last_success_at")
                )

                DB.upsert_format_snapshot(
                    canonical,
                    info["label"],
                    ratings,
                    preserve_existing_state=notification_enabled,
                    deactivate_missing=not notification_enabled,
                    mark_new_pending=(
                        notification_enabled and was_bootstrapped
                    ),
                )
                DB.mark_format_sync(
                    canonical,
                    format_key,
                    success=True,
                    item_count=len(ratings),
                )

                saved += len(ratings)
                succeeded += 1
                print(
                    f"[ARCHIVE] {canonical}/{info['label']}: "
                    f"{len(ratings)} ratings zapisanych."
                )

            except Exception as exc:
                errors += 1
                last_error = f"{type(exc).__name__}: {exc}"
                DB.mark_format_sync(
                    canonical,
                    format_key,
                    success=False,
                    error=last_error,
                )
                print(
                    f"[ARCHIVE] {canonical}/{info['label']}: {last_error}"
                )
                # 429/outage usually affects every route. Even a parser error
                # is safer to retry later than to immediately hammer onward.
                break

        return {
            "formats_due": len(due),
            "formats_attempted": attempted,
            "formats_ok": succeeded,
            "ratings": saved,
            "errors": errors,
            "last_format": last_format,
            "last_error": last_error,
        }

    # ------------------------------------------------------------------
    # Low-priority cache enrichment for configured users
    # ------------------------------------------------------------------

    async def enrich_user(
        self,
        username: str,
        *,
        detail_limit: int,
        release_limit: int,
        priority: int = PRIORITY_MAINTENANCE,
    ) -> dict:
        """Gradually persist release metadata, reviews and Track Ratings.

        Network/rate-limit failures stop the pass because they are global. A
        parser failure for one specific album receives a one-hour cooldown and
        the worker may continue with another candidate on a later pass.
        """
        if not DB.is_monitored(username):
            return {"releases": 0, "details": 0, "errors": 0}

        now = time.time()
        release_done = 0
        detail_done = 0
        errors = 0

        # Pull a few extra candidates so one temporarily broken album does not
        # monopolize position #1 forever.
        release_candidates = DB.release_enrichment_candidates(
            username,
            max(int(release_limit) * 5, int(release_limit)),
        )

        for item in release_candidates:
            if release_done >= max(0, int(release_limit)):
                break

            album_id = str(item.get("album_id") or "")
            if self._release_retry_after.get(album_id, 0.0) > now:
                continue

            try:
                details = await _thread_call(
                    priority,
                    aoty.get_album_details,
                    item.get("url"),
                )
                DB.save_release_details(album_id, details)
                self._release_retry_after.pop(album_id, None)
                release_done += 1
            except (aoty.AOTYRateLimit, requests.RequestException):
                errors += 1
                break
            except Exception as exc:
                errors += 1
                self._release_retry_after[album_id] = time.time() + 60 * 60
                print(
                    f"[CACHE] release {album_id}: "
                    f"{type(exc).__name__}: {exc}"
                )

        detail_candidates = DB.detail_enrichment_candidates(
            username,
            max(int(detail_limit) * 5, int(detail_limit)),
        )

        for item in detail_candidates:
            if detail_done >= max(0, int(detail_limit)):
                break

            album_id = str(item.get("album_id") or "")
            key = (str(username).casefold(), album_id)
            if self._detail_retry_after.get(key, 0.0) > now:
                continue

            try:
                detail = await _thread_call(
                    priority,
                    aoty.get_user_rating_for_album,
                    username,
                    item.get("album_id"),
                    item.get("url"),
                    item.get("release_format"),
                    20,
                    item.get("review_url"),
                    item.get("album"),
                )
                DB.save_rating_detail(username, album_id, detail)
                self._detail_retry_after.pop(key, None)
                detail_done += 1
            except (aoty.AOTYRateLimit, requests.RequestException):
                errors += 1
                break
            except Exception as exc:
                errors += 1
                self._detail_retry_after[key] = time.time() + 60 * 60
                print(
                    f"[CACHE] user detail {username}/{album_id}: "
                    f"{type(exc).__name__}: {exc}"
                )

        return {
            "releases": release_done,
            "details": detail_done,
            "errors": errors,
        }


DATA = DataService()
