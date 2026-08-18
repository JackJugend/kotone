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
import musicbrainz
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
    DETAIL_CHANGE_SCAN_INTERVAL,
    FULL_SYNC_INTERVAL,
    PROFILE_SYNC_INTERVAL,
    PROFILE_RATING_ARCHIVE_FORMATS_PER_CYCLE,
    PROFILE_RATING_ARCHIVE_INTERVAL,
    PROFILE_RATING_ARCHIVE_LIMIT_PER_FORMAT,
    QUICK_RATING_LIMIT_PER_FORMAT,
    MUSICBRAINZ_FALLBACK_ENABLED,
    MUSICBRAINZ_FALLBACK_RETRY_INTERVAL,
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
        # MusicBrainz is a volatile display fallback only. It must never become
        # durable application state or be confused with authoritative AOTY
        # data. Railway restarts intentionally clear this cache.
        self._musicbrainz_release_cache: dict[str, dict] = {}

    @staticmethod
    def _value_present(value) -> bool:
        return value not in (None, "", [], {})

    def _merged_release_details(
        self,
        persistent: dict | None,
        volatile: dict | None,
    ) -> dict | None:
        """Overlay SQLite/AOTY on volatile MusicBrainz and mark each section."""

        if not persistent and not volatile:
            return None

        merged = dict(volatile or {})
        for key, value in dict(persistent or {}).items():
            if self._value_present(value):
                merged[key] = value

        sources: dict[str, str] = {}
        section_keys = {
            "score": ("user_score", "ratings_count"),
            "release_date": ("release_date", "year"),
            "format": ("album_format",),
            "labels": ("label", "labels"),
            "genres": ("genres", "secondary_genres"),
            "vibes": ("vibes",),
            "ranking": ("ranking_year", "year_ranking", "year_ranking_text"),
            "tracklist": ("tracklist",),
        }

        def section_present(data: dict | None, section: str) -> bool:
            data = data or {}
            return any(
                self._value_present(data.get(key))
                for key in section_keys[section]
            )

        for section in section_keys:
            if section_present(volatile, section):
                sources[section] = "musicbrainz"

        durable_source = str(
            (persistent or {}).get("metadata_source") or "aoty"
        ).casefold()
        if durable_source not in {"aoty", "musicbrainz"}:
            durable_source = "aoty"
        for section in section_keys:
            if section_present(persistent, section):
                sources[section] = (
                    "aoty"
                    if section in {"score", "vibes", "ranking"}
                    else durable_source
                )

        if not persistent and volatile:
            merged["metadata_source"] = "musicbrainz"
        merged["metadata_sources"] = sources
        return merged

    def is_monitored(self, username: str) -> bool:
        return DB.is_monitored(username)

    async def _musicbrainz_release_fallback(
        self,
        item: dict,
        *,
        priority: int,
    ) -> dict | None:
        """Fetch public metadata only after AOTY was unavailable.

        This is never called by a Discord command. The result deliberately
        omits AOTY-only values (ratings, rankings, reviews and personal data)
        and remains only in this process's memory; it is never saved to SQLite.
        """
        if not MUSICBRAINZ_FALLBACK_ENABLED:
            return None
        try:
            return await _thread_call(
                priority,
                musicbrainz.MUSICBRAINZ.lookup_release,
                item.get("artist"),
                item.get("album") or item.get("title"),
                requested_format=item.get("release_format"),
            )
        except (
            musicbrainz.MusicBrainzUnavailable,
            aoty.AOTYPageIncomplete,
            aoty.AOTYRateLimit,
            requests.RequestException,
        ) as exc:
            print(f"[MUSICBRAINZ] fallback: {type(exc).__name__}: {exc}")
            return None

    def _rating_with_cached_detail(
        self,
        username: str,
        item: dict | None,
    ) -> dict:
        """Overlay every available SQLite detail onto a command rating.

        Rating-list rows intentionally stay compact in SQLite queries. Commands
        should nevertheless receive cached review text, likes and scored Track
        Ratings without making a live request. A newer live/list value still
        wins for ordinary card fields such as score and date.
        """

        item = dict(item or {})
        album_id = str(item.get("album_id") or "").strip()
        if not album_id or not DB.is_monitored(username):
            return item

        cached = DB.get_rating_detail(username, album_id)
        if cached is None:
            return item

        merged = dict(cached)
        merged.update(
            {
                key: value
                for key, value in item.items()
                if value is not None
            }
        )

        # Compact cards do not contain these rich values. Never let an absent
        # list field erase detail that has already been persisted.
        for key in ("review_url", "review_text"):
            if not item.get(key) and cached.get(key):
                merged[key] = cached[key]
        if not item.get("track_ratings") and cached.get("track_ratings"):
            merged["track_ratings"] = list(cached["track_ratings"])

        # Once a detail page established a baseline, its flags are more
        # authoritative than coarse rating-card markers.
        if cached.get("detail_synced_at") is not None:
            for key in ("has_review", "has_track_ratings", "liked"):
                merged[key] = bool(cached.get(key))
            merged["detail_complete"] = bool(cached.get("detail_complete"))
            merged["detail_incomplete"] = bool(cached.get("detail_incomplete"))

        merged["source"] = item.get("source") or cached.get("source")
        return merged

    def _profile_with_cached_details(
        self,
        username: str,
        profile: dict | None,
    ) -> dict | None:
        if profile is None or not DB.is_monitored(username):
            return profile
        result = dict(profile)
        result["recent_ratings"] = [
            self._rating_with_cached_detail(username, item)
            for item in result.get("recent_ratings") or []
        ]
        return result

    def release_with_cached_details(self, item: dict | None) -> dict:
        """Merge the public release cache into a compact release card."""

        item = dict(item or {})
        album_id = str(item.get("album_id") or "").strip()
        cached = self.cached_release_details(album_id) if album_id else None
        if cached is None:
            return item

        merged = dict(cached)
        merged.update(
            {
                key: value
                for key, value in item.items()
                if value is not None
            }
        )
        title = item.get("title") or item.get("album") or cached.get("album")
        merged["title"] = title
        merged["album"] = title
        merged["release_format"] = (
            item.get("release_format")
            or item.get("album_format")
            or cached.get("album_format")
        )
        merged["album_format"] = merged["release_format"]
        sources = dict(cached.get("metadata_sources") or {})
        item_sources = dict(item.get("metadata_sources") or {})
        item_sections = {
            "score": ("user_score", "ratings_count"),
            "release_date": ("release_date", "year"),
            "format": ("release_format", "album_format"),
            "labels": ("label", "labels"),
            "genres": ("genres", "secondary_genres"),
            "vibes": ("vibes",),
            "ranking": ("ranking_year", "year_ranking", "year_ranking_text"),
            "tracklist": ("tracklist",),
        }
        for section, keys in item_sections.items():
            if any(self._value_present(item.get(key)) for key in keys):
                sources[section] = item_sources.get(section, "aoty")
        merged["metadata_sources"] = sources
        merged["source"] = item.get("source") or "SQLite cache"
        return merged

    def search_cached_artists(self, query: str, limit: int = 10) -> list[dict]:
        """Rank configured-user artists already stored in SQLite."""

        query = str(query or "").strip()
        if not query:
            return []

        ranked = []
        for item in DB.cached_artists():
            score = aoty.fuzzy_match_score(query, item.get("name") or "")
            if score < 0.28:
                continue
            ranked.append(
                {
                    **item,
                    "value": str(item.get("name") or ""),
                    "score": score,
                    "source": "SQLite cache",
                }
            )

        ranked.sort(
            key=lambda item: (
                -float(item.get("score") or 0),
                str(item.get("name") or "").casefold(),
            )
        )
        return ranked[: max(1, int(limit))]

    async def search_artists(self, query: str, limit: int = 10) -> list[dict]:
        """Artist autocomplete from the durable SQLite cache only."""

        query = str(query or "").strip()
        limit = max(1, min(25, int(limit)))
        local = self.search_cached_artists(query, limit=limit)

        return local

    def cached_artist_discography(
        self,
        artist_query: str,
    ) -> tuple[dict | None, dict | None]:
        """Resolve an artist and their known releases entirely from SQLite."""

        candidates = self.search_cached_artists(artist_query, limit=1)
        if not candidates:
            return None, None

        artist_info = dict(candidates[0])
        releases = DB.cached_artist_releases(artist_info["name"])
        if not releases:
            return None, None

        artist_url = artist_info.get("url") or releases[0].get("artist_url")
        artist_info["url"] = artist_url
        cached_genres = sorted(
            {
                str(genre).strip()
                for release in releases
                for genre in (release.get("genres") or [])
                if str(genre).strip()
            },
            key=str.casefold,
        )
        return artist_info, {
            "artist": artist_info["name"],
            "url": artist_url,
            "image": None,
            "releases": releases,
            "genres_text": ", ".join(cached_genres[:10]) or None,
            "source": "SQLite cache",
        }

    async def get_artist_discography(
        self,
        artist_query: str,
        *,
        prefer_cached: bool = False,
    ) -> tuple[dict | None, dict | None]:
        """Load an artist discography exclusively from SQLite.

        AOTY is refreshed by the monitor/background worker, never by a
        command or command autocomplete.
        """

        cached_info, cached_discography = self.cached_artist_discography(
            artist_query
        )
        # Interactive commands are SQLite-only. The background worker is the
        # sole owner of regular AOTY refreshes, so an existing local artist
        # cache always wins regardless of its age.
        return cached_info, cached_discography

    @staticmethod
    def _age(timestamp: float | None) -> float:
        if not timestamp:
            return float("inf")
        return max(0.0, time.time() - float(timestamp))

    @staticmethod
    def _enabled_format_labels() -> set[str]:
        return {
            str(RATING_FORMATS[key]["label"]).casefold()
            for key, value in RATING_FETCH_LIMITS.items()
            if int(value or 0) > 0 and key in RATING_FORMATS
        }

    @classmethod
    def _notification_enabled_for_item(
        cls,
        item: dict,
        *,
        requested_format_key: str | None = None,
    ) -> bool:
        if requested_format_key and requested_format_key != "all":
            if requested_format_key in RATING_FORMATS:
                return int(RATING_FETCH_LIMITS.get(requested_format_key, 0) or 0) > 0

        release_format = item.get("release_format") or item.get("album_format")
        if not release_format:
            # Missing format metadata is ambiguous.  Treat it as monitored so
            # an interactive cache refresh can never consume a notification.
            return True
        return str(release_format).casefold() in cls._enabled_format_labels()

    @staticmethod
    def _monitor_has_baseline(username: str) -> bool:
        return bool(DB.sync_timestamps(username).get("ratings_synced_at"))

    def _persist_non_notification_ratings(
        self,
        username: str,
        ratings: list[dict],
        *,
        source: str,
    ) -> None:
        """Persist disabled-format ratings and retain post-baseline history."""

        record_changes = self._monitor_has_baseline(username)
        for item in ratings:
            DB.upsert_rating(
                username,
                item,
                record_history=record_changes,
                record_changes=record_changes,
                source=source,
            )

    def _persist_interactive_ratings_safely(
        self,
        username: str,
        ratings: list[dict],
        *,
        requested_format_key: str | None,
    ) -> None:
        """Cache interactive results without stealing monitor-owned scores.

        Existing rows in notification-enabled formats are deliberately left
        untouched.  New rows may be seeded for offline use, but after the
        monitor baseline they are marked pending so Discord delivery remains
        at-least-once.  Disabled formats have no Discord event to protect and
        therefore update normally with post-baseline change history.
        """

        existing = DB.get_ratings_map(username, include_inactive=True)
        has_baseline = self._monitor_has_baseline(username)

        for item in ratings:
            album_id = str(item.get("album_id") or "").strip()
            if not album_id:
                continue

            if not self._notification_enabled_for_item(
                item,
                requested_format_key=requested_format_key,
            ):
                DB.upsert_rating(
                    username,
                    item,
                    record_history=has_baseline,
                    record_changes=has_baseline,
                    source="interactive_recent_disabled",
                )
                continue

            previous = existing.get(album_id)
            if previous is None:
                DB.upsert_rating(
                    username,
                    item,
                    record_history=False,
                    record_changes=False,
                    source="interactive_recent_seed",
                )
                if has_baseline:
                    DB.set_notify_pending(username, album_id, True)
                continue

            # An inactive row represents a removal/restoration boundary.  Keep
            # its old score/active state for the monitor, but make sure a later
            # monitor pass treats the live rediscovery as pending.
            if has_baseline and not previous.get("active", True):
                DB.set_notify_pending(username, album_id, True)

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

        return False

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

            # A valid but partially rendered profile may omit the whole
            # Favorites section. Preserve the last authoritative list instead
            # of recording a false mass removal.
            if not profile.pop("favorites_complete", True):
                cached = DB.get_profile(username, recent_limit=1)
                if cached is not None:
                    profile["favorite_kind"] = cached.get("favorite_kind")
                    profile["favorites"] = list(cached.get("favorites") or [])
                    profile["favorite_albums"] = list(
                        cached.get("favorite_albums") or []
                    )
                    profile["favorite_artists"] = list(
                        cached.get("favorite_artists") or []
                    )

            DB.save_profile(username, profile)
            return profile
        except Exception as exc:
            DB.mark_sync_error(username, f"profile: {type(exc).__name__}: {exc}")
            raise

    async def get_profile(
        self,
        username: str,
        *,
        recent_limit: int = 50,
        allow_network: bool = True,
    ) -> dict:
        """Return a cached profile, or one lightweight non-persistent lookup.

        Profiles outside config.json are deliberately never written to SQLite.
        Their profile page and compact recent-rating cards are fetched once from
        AOTY; each card is then hydrated only from the existing public release
        cache.  No extra album/detail requests are made.
        """

        username = str(username).strip()

        if not DB.is_monitored(username):
            if not allow_network:
                raise ValueError(
                    "Profil nie jest w lokalnej bazie SQLite; "
                    "Kotone nie sprawdza AOTY w komendach."
                )
            profile = await _thread_call(
                PRIORITY_INTERACTIVE,
                aoty.get_profile_data,
                username,
                recent_limit,
            )
            result = dict(profile)
            result["recent_ratings"] = [
                self.release_with_cached_details(item)
                for item in profile.get("recent_ratings") or []
            ]
            return result

        cached = self._profile_with_cached_details(
            username,
            DB.get_profile(username, recent_limit=recent_limit),
        )

        def with_sqlite_average(profile: dict | None) -> dict | None:
            if profile is None:
                return None
            average, count = DB.get_rating_average(username)
            result = dict(profile)
            result["sqlite_average_rating"] = average
            result["sqlite_average_count"] = count
            return result

        cached = with_sqlite_average(cached)
        if cached is not None:
            return cached
        raise aoty.AOTYUserNotFound()

    async def get_avatar(self, username: str) -> str | None:
        if DB.is_monitored(username):
            return DB.get_avatar(username)
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
            ratings = await _thread_call(
                priority,
                aoty.get_ratings,
                username,
                None,
                RATING_FETCH_LIMITS,
            )
            if getattr(ratings, "stale", False):
                raise aoty.AOTYStalePage(
                    f"Pełny monitor {username} otrzymał wyłącznie stale cache."
                )
            return ratings

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

        if getattr(recent, "stale", False):
            raise aoty.AOTYStalePage(
                f"Szybki monitor {username} otrzymał wyłącznie stale cache."
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
                self._persist_non_notification_ratings(
                    username,
                    silent_items,
                    source="monitor_disabled_format",
                )

        if not enabled_labels:
            return []

        # Cards from the combined route normally contain their exact format.
        # If AOTY temporarily omits the label, keep the item rather than miss a
        # genuine new rating; the next full sync will normalize its format.
        return aoty.RatingsResult(
            [
                item
                for item in recent
                if not item.get("release_format")
                or str(item.get("release_format")).casefold() in enabled_labels
            ],
            stale=False,
        )

    async def get_recent_ratings(
        self,
        username: str,
        count: int = 20,
        format_key: str = "all",
        *,
        allow_network: bool = True,
        **filters,
    ) -> list[dict]:
        username = str(username).strip()
        count = max(1, min(50, int(count)))

        if not DB.is_monitored(username):
            if not allow_network:
                return []
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

        cached = [
            self._rating_with_cached_detail(username, item)
            for item in DB.get_recent_ratings(
                username,
                count,
                release_format=format_label,
                **filters,
            )
        ]
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

        # Command output is an SQLite snapshot. The legacy opt-in live path is
        # retained for background/internal callers and regression coverage;
        # every Discord command explicitly passes allow_network=False.
        if not allow_network:
            return cached

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

            # A stale transport page is still useful for display, but never as
            # a new persistence/notification decision. Prefer SQLite when it
            # already has the configured user's durable state.
            if getattr(live, "stale", False):
                return cached or [
                    self._rating_with_cached_detail(username, item)
                    for item in live[:count]
                ]

            if not live and cached:
                # Empty partial reads are not authoritative for removals; the
                # comprehensive per-format archive owns that decision.
                return cached

            # Partial command refresh: never mark other rows inactive and, for
            # notification-enabled formats, never overwrite the monitor-owned
            # score or consume notify_pending.
            self._persist_interactive_ratings_safely(
                username,
                list(live),
                requested_format_key=format_key,
            )
            return [
                self._rating_with_cached_detail(username, item)
                for item in live[:count]
            ]
        except (
            aoty.AOTYRateLimit,
            aoty.AOTYPageIncomplete,
            requests.RequestException,
        ):
            if cached:
                return cached
            raise

    def cached_rating(self, username: str, album_id: str) -> dict | None:
        if not DB.is_monitored(username):
            return None
        cached = DB.get_rating_detail(username, album_id)
        return (
            self._rating_with_cached_detail(username, cached)
            if cached is not None
            else None
        )

    def cached_user_track_ratings(self, username: str, album_id: str) -> list[dict]:
        """Read durable personal Track Ratings without an AOTY request."""

        if not DB.is_monitored(username):
            return []
        return DB.get_user_track_ratings(username, album_id)

    def cached_release_details(self, album_id: str) -> dict | None:
        """Return SQLite plus volatile fallback without making any request."""
        album_id = str(album_id or "")
        return self._merged_release_details(
            DB.get_release_details(album_id),
            self._musicbrainz_release_cache.get(album_id),
        )

    def cached_genres(self, username: str | None = None) -> list[str]:
        """Distinct cached genres for command autocomplete, without HTTP."""
        return DB.available_genres(username)

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
        allow_network: bool = True,
    ) -> dict:
        """One user's rating for one album, cached persistently only for config users."""

        monitored = DB.is_monitored(username)
        if monitored:
            # Even compact command cards receive every detail already stored in
            # SQLite. ``require_detail`` only decides whether missing/stale
            # detail should trigger a live refresh.
            cached = DB.get_rating_detail(username, album_id)
        else:
            cached = None

        if cached is not None and not allow_network:
            return cached

        if not allow_network:
            return {
                "album_id": str(album_id or ""),
                "score": None,
                "detail_incomplete": True,
                "source": "SQLite cache",
            }

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

            if live.get("detail_incomplete") and cached is not None:
                return cached

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

                    monitor_initialized = bool(
                        DB.sync_timestamps(username).get("ratings_synced_at")
                    )
                    enabled_labels = {
                        str(RATING_FORMATS[key]["label"]).casefold()
                        for key, value in RATING_FETCH_LIMITS.items()
                        if int(value or 0) > 0 and key in RATING_FORMATS
                    }
                    notification_enabled = bool(
                        release_format
                        and str(release_format).casefold() in enabled_labels
                    )

                    # Interactive discovery must never consume a later monitor
                    # notification. Enabled formats become pending after the
                    # cache seed; disabled formats can safely enter history now.
                    DB.upsert_rating(
                        username,
                        seed,
                        record_history=False,
                        record_changes=(
                            monitor_initialized and not notification_enabled
                        ),
                        source="interactive_discovery",
                    )
                    if monitor_initialized and notification_enabled:
                        DB.set_notify_pending(username, album_id, True)

                if DB.get_rating(username, album_id) is not None:
                    DB.save_rating_detail(
                        username,
                        album_id,
                        live,
                        record_changes=True,
                        source="interactive_detail",
                    )

            return live

        except (
            aoty.AOTYRateLimit,
            aoty.AOTYPageIncomplete,
            requests.RequestException,
        ):
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
        allow_network: bool = True,
    ) -> dict:
        item = dict(item or {})
        album_id = str(item.get("album_id") or "")
        url = item.get("url")
        # Public release cache is safe to reuse from every command. Database
        # scope enforcement still guarantees that a release is persisted only
        # when at least one configured user has rated it.
        cached = self.cached_release_details(album_id) if album_id else None
        if cached is not None:
            return cached

        if not allow_network:
            return {}

        if not url:
            return cached or {}

        try:
            details = await _thread_call(
                PRIORITY_NORMAL,
                aoty.get_album_details,
                url,
            )
            details = dict(details or {})
            details["source"] = "aoty"
            if album_id:
                # save_release_details() itself checks whether the album is in
                # the configured-user scope. Public searches are never enough
                # to create a persistent row on their own.
                if DB.save_release_details(album_id, details):
                    self._musicbrainz_release_cache.pop(album_id, None)
                    # Parser snapshots carry per-section completeness. Return
                    # the same non-destructively merged view that was stored so
                    # a partially rendered AOTY page cannot temporarily strip
                    # metadata or tracks from an interactive command either.
                    merged = DB.get_release_details(album_id)
                    if merged is not None:
                        return merged
            return details
        except (
            aoty.AOTYRateLimit,
            aoty.AOTYPageIncomplete,
            requests.RequestException,
        ):
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
                if (
                    not ratings
                    and int(previous_archive.get("item_count") or 0) > 0
                ):
                    raise aoty.AOTYArchiveIncomplete(
                        f"Pusty snapshot {canonical}/{format_key} po wcześniejszych "
                        f"{int(previous_archive.get('item_count') or 0)} ocenach; "
                        "format nie zostanie oznaczony jako pełny."
                    )
                was_bootstrapped = bool(
                    previous_archive.get("last_success_at")
                )

                DB.upsert_format_snapshot(
                    canonical,
                    info["label"],
                    ratings,
                    preserve_existing_state=notification_enabled,
                    # The per-format route is comprehensive. A non-empty
                    # successful snapshot therefore owns membership even for
                    # notification-enabled formats. Empty parser results stay
                    # non-destructive inside Database.upsert_format_snapshot.
                    deactivate_missing=True,
                    mark_new_pending=(
                        notification_enabled and was_bootstrapped
                    ),
                    # First bootstrap establishes historical baseline only.
                    # Every later full-format snapshot becomes part of the
                    # persistent change audit trail.
                    record_history=was_bootstrapped,
                    record_changes=was_bootstrapped,
                    source="archive",
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
        musicbrainz_only: bool = False,
    ) -> dict:
        """Gradually enrich public display data and persist AOTY user details.

        Network/rate-limit failures stop the pass because they are global. A
        parser failure for one specific album receives a one-hour cooldown and
        the worker may continue with another candidate on a later pass.
        """
        if not DB.is_monitored(username):
            return {"releases": 0, "details": 0, "errors": 0}

        now = time.time()
        release_done = 0
        musicbrainz_done = 0
        detail_done = 0
        errors = 0

        # Personal Track Ratings are more valuable than generic album metadata
        # and were previously starved: a blocked release fetch returned before
        # this pass ever reached the user-detail phase.  If even one detail is
        # due, finish that queue first; public release enrichment resumes once
        # it is empty.
        # A MusicBrainz-only pass cannot resolve personal AOTY details. Do not
        # let stale reviews/Track Ratings permanently starve the independent
        # public fallback queue while /dbonly or a challenge is active.
        priority_details = (
            []
            if musicbrainz_only
            else DB.detail_enrichment_candidates(
                username,
                1,
                stale_before=time.time() - DETAIL_CHANGE_SCAN_INTERVAL,
            )
        )
        release_candidates = (
            []
            if priority_details
            else DB.release_enrichment_candidates(
                username,
                max(int(release_limit) * 5, int(release_limit)),
                fallback_stale_before=(
                    now - MUSICBRAINZ_FALLBACK_RETRY_INTERVAL
                ),
                aoty_stale_before=(
                    now if musicbrainz_only else now - RELEASE_DETAIL_TTL
                ),
                exclude_album_ids=(
                    album_id
                    for album_id, retry_after in self._release_retry_after.items()
                    if retry_after > now
                ),
            )
        )

        aoty_unavailable = bool(musicbrainz_only)

        for item in release_candidates:
            if release_done >= max(0, int(release_limit)):
                break

            album_id = str(item.get("album_id") or "")
            if self._release_retry_after.get(album_id, 0.0) > now:
                continue

            if aoty_unavailable:
                details = await self._musicbrainz_release_fallback(
                    item,
                    priority=priority,
                )
                if details:
                    self._musicbrainz_release_cache[album_id] = dict(details)
                    self._release_retry_after[album_id] = (
                        time.time() + MUSICBRAINZ_FALLBACK_RETRY_INTERVAL
                    )
                    release_done += 1
                    musicbrainz_done += 1
                elif details is None:
                    self._release_retry_after[album_id] = time.time() + 60 * 60
                continue

            try:
                details = await _thread_call(
                    priority,
                    aoty.get_album_details,
                    item.get("url"),
                )
                details = dict(details or {})
                details["source"] = "aoty"
                DB.save_release_details(album_id, details)
                self._musicbrainz_release_cache.pop(album_id, None)
                self._release_retry_after.pop(album_id, None)
                release_done += 1
            except (
                aoty.AOTYChallengeCooldown,
                aoty.AOTYRateLimit,
                aoty.AOTYPageIncomplete,
                requests.RequestException,
            ):
                # AOTY is authoritative. Only when it cannot provide a safe
                # page do we fill public gaps from MusicBrainz, and then stop
                # probing AOTY again for the rest of this worker pass.
                aoty_unavailable = True
                details = await self._musicbrainz_release_fallback(
                    item,
                    priority=priority,
                )
                if details:
                    self._musicbrainz_release_cache[album_id] = dict(details)
                    self._release_retry_after[album_id] = (
                        time.time() + MUSICBRAINZ_FALLBACK_RETRY_INTERVAL
                    )
                    release_done += 1
                    musicbrainz_done += 1
                    continue
                errors += 1
                return {
                    "releases": release_done,
                    "musicbrainz": musicbrainz_done,
                    "details": detail_done,
                    "errors": errors,
                }
            except Exception as exc:
                errors += 1
                self._release_retry_after[album_id] = time.time() + 60 * 60
                print(
                    f"[CACHE] release {album_id}: "
                    f"{type(exc).__name__}: {exc}"
                )

        # MusicBrainz has no personal AOTY reviews/likes/Track Ratings. Those
        # checks wait for AOTY instead of being falsely treated as complete.
        if aoty_unavailable:
            return {
                "releases": release_done,
                "musicbrainz": musicbrainz_done,
                "details": detail_done,
                "errors": errors,
            }

        detail_candidates = DB.detail_enrichment_candidates(
            username,
            max(int(detail_limit) * 5, int(detail_limit)),
            stale_before=time.time() - DETAIL_CHANGE_SCAN_INTERVAL,
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
                complete = DB.save_rating_detail(
                    username,
                    album_id,
                    detail,
                    record_changes=True,
                    source="background_detail",
                )
                if complete:
                    self._detail_retry_after.pop(key, None)
                    detail_done += 1
                else:
                    # Incomplete pages are intentionally non-destructive, but
                    # without a cooldown they would stay candidate #1 forever.
                    self._detail_retry_after[key] = time.time() + 60 * 60
            except (
                aoty.AOTYChallengeCooldown,
                aoty.AOTYRateLimit,
                requests.RequestException,
            ):
                errors += 1
                return {
                    "releases": release_done,
                    "details": detail_done,
                    "errors": errors,
                }
            except Exception as exc:
                errors += 1
                self._detail_retry_after[key] = time.time() + 60 * 60
                print(
                    f"[CACHE] user detail {username}/{album_id}: "
                    f"{type(exc).__name__}: {exc}"
                )

        return {
            "releases": release_done,
            "musicbrainz": musicbrainz_done,
            "details": detail_done,
            "errors": errors,
        }


DATA = DataService()
