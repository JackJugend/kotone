"""Small independent SQLite archive for public Last.fm listening history.

This database is intentionally separate from ``kotone.sqlite3``.  Kotone's
AOTY state may be migrated or restored independently, while an incremental
scrobble archive must keep its long-running newest-to-oldest cursor.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterable

from lastfm_identity import album_key, artist_key, track_key
from settings import LASTFM_DATABASE_FILE


class LastFMDatabase:
    def __init__(self, path: str = LASTFM_DATABASE_FILE) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(
            self.path,
            check_same_thread=False,
        )
        self.connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock, self.connection:
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA busy_timeout=30000")
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS profiles (
                    profile_key TEXT PRIMARY KEY,
                    lastfm_username TEXT NOT NULL,
                    profile_url TEXT,
                    avatar_url TEXT,
                    total_scrobbles INTEGER,
                    artist_count INTEGER,
                    album_count INTEGER,
                    track_count INTEGER,
                    registered_at INTEGER,
                    fetched_at REAL NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS import_state (
                    profile_key TEXT PRIMARY KEY,
                    next_page INTEGER NOT NULL DEFAULT 1,
                    total_pages INTEGER,
                    total_scrobbles INTEGER,
                    complete INTEGER NOT NULL DEFAULT 0,
                    last_page_at REAL NOT NULL DEFAULT 0,
                    last_newest_at REAL NOT NULL DEFAULT 0,
                    last_error TEXT
                );

                CREATE TABLE IF NOT EXISTS scrobbles (
                    profile_key TEXT NOT NULL,
                    played_at INTEGER NOT NULL,
                    artist TEXT NOT NULL,
                    album TEXT,
                    track TEXT NOT NULL,
                    artist_mbid TEXT,
                    album_mbid TEXT,
                    track_mbid TEXT,
                    artist_key TEXT,
                    album_key TEXT,
                    track_key TEXT,
                    aoty_album_id TEXT,
                    url TEXT,
                    PRIMARY KEY (profile_key, played_at, artist, track)
                );
                CREATE INDEX IF NOT EXISTS idx_lastfm_scrobbles_recent
                    ON scrobbles(profile_key, played_at DESC);
                """
            )
            self._ensure_column("scrobbles", "aoty_album_id", "TEXT")
            self._ensure_column("scrobbles", "artist_key", "TEXT")
            self._ensure_column("scrobbles", "album_key", "TEXT")
            self._ensure_column("scrobbles", "track_key", "TEXT")
            self._ensure_column("import_state", "last_newest_at", "REAL NOT NULL DEFAULT 0")
            self._backfill_identity_keys_locked()
            self.connection.execute(
                """CREATE INDEX IF NOT EXISTS idx_lastfm_scrobbles_aoty_album
                ON scrobbles(aoty_album_id)"""
            )
            self.connection.execute(
                """CREATE INDEX IF NOT EXISTS idx_lastfm_scrobbles_identity
                ON scrobbles(profile_key, artist_key, album_key, track_key)"""
            )

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        columns = {
            str(row["name"])
            for row in self.connection.execute(f"PRAGMA table_info({table})")
        }
        if column not in columns:
            self.connection.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
            )

    def _backfill_identity_keys_locked(self) -> None:
        """Give pre-alias archives stable keys once, without touching display text."""

        rows = self.connection.execute(
            """SELECT rowid, artist, album, track, artist_mbid, album_mbid,
            track_mbid FROM scrobbles
            WHERE artist_key IS NULL OR album_key IS NULL OR track_key IS NULL"""
        ).fetchall()
        for row in rows:
            self.connection.execute(
                """UPDATE scrobbles
                SET artist_key = ?, album_key = ?, track_key = ?
                WHERE rowid = ?""",
                (
                    artist_key(row["artist"], row["artist_mbid"]),
                    album_key(
                        row["artist"],
                        row["album"],
                        artist_mbid=row["artist_mbid"],
                        album_mbid=row["album_mbid"],
                    ),
                    track_key(row["track"], row["track_mbid"]),
                    row["rowid"],
                ),
            )

    @staticmethod
    def _key(profile_key: object) -> str:
        return str(profile_key or "").strip().casefold()

    @staticmethod
    def _integer(value: object) -> int | None:
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None

    def save_profile(self, profile_key: object, data: dict) -> None:
        key = self._key(profile_key)
        if not key:
            return
        with self._lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO profiles(
                    profile_key, lastfm_username, profile_url, avatar_url,
                    total_scrobbles, artist_count, album_count, track_count,
                    registered_at, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(profile_key) DO UPDATE SET
                    lastfm_username=excluded.lastfm_username,
                    profile_url=excluded.profile_url,
                    avatar_url=excluded.avatar_url,
                    total_scrobbles=excluded.total_scrobbles,
                    artist_count=excluded.artist_count,
                    album_count=excluded.album_count,
                    track_count=excluded.track_count,
                    registered_at=excluded.registered_at,
                    fetched_at=excluded.fetched_at
                """,
                (
                    key,
                    str(data.get("lastfm_username") or key),
                    data.get("url"),
                    data.get("avatar_url"),
                    self._integer(data.get("total_scrobbles")),
                    self._integer(data.get("artist_count")),
                    self._integer(data.get("album_count")),
                    self._integer(data.get("track_count")),
                    self._integer(data.get("registered_at")),
                    time.time(),
                ),
            )

    def profile_due(self, profile_key: object, interval_seconds: float) -> bool:
        key = self._key(profile_key)
        with self._lock:
            row = self.connection.execute(
                "SELECT fetched_at FROM profiles WHERE profile_key = ?", (key,)
            ).fetchone()
        return row is None or time.time() - float(row["fetched_at"] or 0) >= interval_seconds

    def get_profile(self, profile_key: object) -> dict | None:
        key = self._key(profile_key)
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM profiles WHERE profile_key = ?", (key,)
            ).fetchone()
        return dict(row) if row else None

    def state(self, profile_key: object) -> dict:
        key = self._key(profile_key)
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM import_state WHERE profile_key = ?", (key,)
            ).fetchone()
        return dict(row) if row else {
            "profile_key": key,
            "next_page": 1,
            "total_pages": None,
            "total_scrobbles": None,
            "complete": 0,
            "last_page_at": 0.0,
            "last_newest_at": 0.0,
            "last_error": None,
        }

    def newest_due(self, profile_key: object, interval_seconds: float) -> bool:
        """Return whether page one should be refreshed before older history."""

        state = self.state(profile_key)
        return time.time() - float(state.get("last_newest_at") or 0) >= max(
            0.0, float(interval_seconds)
        )

    def import_page(self, profile_key: object, page: dict) -> int:
        """Persist one newest-to-oldest API page and advance its cursor."""

        key = self._key(profile_key)
        number = max(1, int(page.get("page") or 1))
        total_pages = max(1, int(page.get("total_pages") or 1))
        rows: Iterable[dict] = page.get("tracks") or []
        inserted = 0
        with self._lock, self.connection:
            for track in rows:
                inserted += self._insert_track_locked(key, track)
            next_page = number + 1
            complete = int(next_page > total_pages)
            self.connection.execute(
                """
                INSERT INTO import_state(
                    profile_key, next_page, total_pages, total_scrobbles,
                    complete, last_page_at, last_newest_at, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(profile_key) DO UPDATE SET
                    next_page=excluded.next_page,
                    total_pages=excluded.total_pages,
                    total_scrobbles=excluded.total_scrobbles,
                    complete=excluded.complete,
                    last_page_at=excluded.last_page_at,
                    last_newest_at=CASE
                        WHEN excluded.last_newest_at > 0 THEN excluded.last_newest_at
                        ELSE import_state.last_newest_at
                    END,
                    last_error=NULL
                """,
                (
                    key,
                    next_page,
                    total_pages,
                    max(0, int(page.get("total") or 0)),
                    complete,
                    time.time(),
                    time.time() if number == 1 else 0.0,
                ),
            )
        return inserted

    def refresh_newest_page(self, profile_key: object, page: dict) -> int:
        """Merge fresh page one after the full archive has completed.

        This deliberately leaves the historical cursor complete: future syncs
        only add recently played tracks instead of crawling every old page
        again.
        """

        key = self._key(profile_key)
        inserted = 0
        with self._lock, self.connection:
            for track in page.get("tracks") or []:
                inserted += self._insert_track_locked(key, track)
            self.connection.execute(
                """UPDATE import_state
                SET last_page_at = ?, last_newest_at = ?, last_error = NULL
                WHERE profile_key = ?""",
                (time.time(), time.time(), key),
            )
        return inserted

    def _insert_track_locked(self, profile_key: str, track: dict) -> int:
        """Add one scrobble without replacing an existing record."""

        played_at = int(track["played_at"])
        track_mbid = str(track.get("track_mbid") or "").strip() or None
        resolved_artist_key = artist_key(track["artist"], track.get("artist_mbid"))
        resolved_album_key = album_key(
            track["artist"],
            track.get("album"),
            artist_mbid=track.get("artist_mbid"),
            album_mbid=track.get("album_mbid"),
        )
        resolved_track_key = track_key(track["track"], track_mbid)
        if track_mbid and self.connection.execute(
            """
            SELECT 1 FROM scrobbles
            WHERE profile_key = ? AND played_at = ? AND track_mbid = ? LIMIT 1
            """,
            (profile_key, played_at, track_mbid),
        ).fetchone():
            return 0
        if self.connection.execute(
            """SELECT 1 FROM scrobbles
            WHERE profile_key = ? AND played_at = ?
              AND artist_key = ? AND album_key = ? AND track_key = ? LIMIT 1""",
            (
                profile_key,
                played_at,
                resolved_artist_key,
                resolved_album_key,
                resolved_track_key,
            ),
        ).fetchone():
            return 0
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO scrobbles(
                profile_key, played_at, artist, album, track,
                artist_mbid, album_mbid, track_mbid, artist_key, album_key,
                track_key, aoty_album_id, url
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                profile_key,
                played_at,
                str(track["artist"]),
                track.get("album"),
                str(track["track"]),
                track.get("artist_mbid"),
                track.get("album_mbid"),
                track_mbid,
                resolved_artist_key,
                resolved_album_key,
                resolved_track_key,
                track.get("aoty_album_id"),
                track.get("url"),
            ),
        )
        return max(0, int(cursor.rowcount or 0))

    def import_tracks(self, profile_key: object, tracks: Iterable[dict]) -> int:
        """Import attachment rows without changing the API pagination cursor."""

        key = self._key(profile_key)
        inserted = 0
        with self._lock, self.connection:
            for track in tracks:
                inserted += self._insert_track_locked(key, track)
        return inserted

    def mark_imported_complete(self, profile_key: object) -> None:
        """Mark an offline CSV archive as complete without annotating rows.

        A user-provided export is already a complete history snapshot for the
        purpose of the background worker.  We therefore avoid immediately
        crawling the same history through Last.fm again.  The worker still
        refreshes page one at its normal, deliberately infrequent interval.
        """

        key = self._key(profile_key)
        if not key:
            return
        archived = self.archive_statistics(key)
        now = time.time()
        with self._lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO import_state(
                    profile_key, next_page, total_pages, total_scrobbles,
                    complete, last_page_at, last_newest_at, last_error
                ) VALUES (?, 1, 1, ?, 1, ?, ?, NULL)
                ON CONFLICT(profile_key) DO UPDATE SET
                    next_page = 1,
                    total_pages = CASE
                        WHEN import_state.total_pages IS NULL THEN 1
                        ELSE import_state.total_pages
                    END,
                    total_scrobbles = CASE
                        WHEN import_state.total_scrobbles IS NULL THEN
                            excluded.total_scrobbles
                        ELSE import_state.total_scrobbles
                    END,
                    complete = 1,
                    last_page_at = excluded.last_page_at,
                    last_newest_at = excluded.last_newest_at,
                    last_error = NULL
                """,
                (key, int(archived["scrobbles"]), now, now),
            )

    def mark_error(self, profile_key: object, error: object) -> None:
        key = self._key(profile_key)
        state = self.state(key)
        with self._lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO import_state(profile_key, next_page, complete, last_error)
                VALUES (?, ?, 0, ?)
                ON CONFLICT(profile_key) DO UPDATE SET last_error=excluded.last_error
                """,
                (key, int(state.get("next_page") or 1), str(error or "")),
            )

    def latest_scrobble(self, profile_key: object) -> dict | None:
        key = self._key(profile_key)
        with self._lock:
            row = self.connection.execute(
                """
                SELECT * FROM scrobbles WHERE profile_key = ?
                ORDER BY played_at DESC LIMIT 1
                """,
                (key,),
            ).fetchone()
        return dict(row) if row else None

    def archive_statistics(self, profile_key: object) -> dict[str, int]:
        """Return counters calculated solely from Kotone's stored scrobbles."""

        key = self._key(profile_key)
        with self._lock:
            row = self.connection.execute(
                """
                SELECT
                    COUNT(*) AS scrobbles,
                    COUNT(DISTINCT artist_key) AS artists,
                    COUNT(DISTINCT artist_key || '\u001f' || track_key) AS tracks,
                    COUNT(DISTINCT CASE
                        WHEN TRIM(COALESCE(album, '')) <> ''
                        THEN album_key
                    END) AS albums
                FROM scrobbles WHERE profile_key = ?
                """,
                (key,),
            ).fetchone()
        return {
            "scrobbles": int(row["scrobbles"] or 0),
            "artists": int(row["artists"] or 0),
            "albums": int(row["albums"] or 0),
            "tracks": int(row["tracks"] or 0),
        }

    def archive_progress(self, profile_key: object) -> dict[str, int | bool | None]:
        """Return explicit import progress without conflating it with library totals.

        Last.fm's profile counters describe its own library/account.  Kotone's
        archive counters describe distinct names encountered in stored
        listening history, so they are intentionally not interchangeable.
        """

        state = self.state(profile_key)
        archived = self.archive_statistics(profile_key)
        total = self._integer(state.get("total_scrobbles"))
        total_pages = self._integer(state.get("total_pages"))
        next_page = max(1, self._integer(state.get("next_page")) or 1)
        return {
            "scrobbles": archived["scrobbles"],
            "total_scrobbles": total,
            "total_pages": total_pages,
            "next_page": next_page,
            "complete": bool(state.get("complete")),
        }

    def artist_scrobble_count(
        self,
        profile_key: object,
        artist: object,
        *,
        artist_mbid: object = None,
    ) -> int:
        """Count one artist from archive, preferring an exact MusicBrainz ID."""

        key = self._key(profile_key)
        identity = artist_key(artist, artist_mbid)
        with self._lock:
            row = self.connection.execute(
                """SELECT COUNT(*) AS count FROM scrobbles
                WHERE profile_key = ? AND artist_key = ?""",
                (key, identity),
            ).fetchone()
        return int(row["count"] or 0)

    def album_scrobble_count(
        self,
        profile_key: object,
        album: object,
        *,
        artist: object = None,
        artist_mbid: object = None,
        album_mbid: object = None,
        aoty_album_id: object = None,
    ) -> int:
        """Count one album from archive, preferring AOTY then MusicBrainz IDs."""

        key = self._key(profile_key)
        aoty_id = str(aoty_album_id or "").strip()
        identity = album_key(
            artist,
            album,
            artist_mbid=artist_mbid,
            album_mbid=album_mbid,
        )
        with self._lock:
            if aoty_id:
                row = self.connection.execute(
                    """SELECT COUNT(*) AS count FROM scrobbles
                    WHERE profile_key = ? AND aoty_album_id = ?""",
                    (key, aoty_id),
                ).fetchone()
                count = int(row["count"] or 0)
                if count:
                    return count
            row = self.connection.execute(
                """SELECT COUNT(*) AS count FROM scrobbles
                WHERE profile_key = ? AND album_key = ?""",
                (key, identity),
            ).fetchone()
        return int(row["count"] or 0)


LASTFM_DB = LastFMDatabase()
