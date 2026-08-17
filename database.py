"""SQLite persistence layer for Kotone.

Only AOTY users explicitly listed in ``config.json -> users`` are persisted.
Commands may still *read* arbitrary public AOTY pages, but those users are not
written to this database.

The database is intentionally richer than the old data.json state:
- full profile summary + favorites + rating distribution;
- all monitored ratings and their flags;
- review / track-rating details once fetched;
- score-change history;
- public release metadata only for releases rated by monitored users;
- sync timestamps/errors so commands can decide whether cached data is fresh.

All schema migrations are additive and run automatically on startup.  This
allows the bot to upgrade an existing Railway volume in place.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from copy import deepcopy
from threading import RLock
from typing import Iterable

from settings import (
    DATABASE_BACKUP_FILE,
    DATABASE_FILE,
    DATA_FILE,
    LOCAL_DATABASE_BACKUP_INTERVAL,
    MIGRATED_DATA_BACKUP_FILE,
    USERS,
)

SCHEMA_VERSION = 9


def _json_dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_load(value, default):
    if value in (None, ""):
        return deepcopy(default)
    try:
        return json.loads(value)
    except Exception:
        return deepcopy(default)


def _bool_int(value) -> int:
    return 1 if bool(value) else 0


def _now() -> float:
    return time.time()


class Database:
    """Single-process SQLite repository.

    Railway runs one Kotone replica, while Discord callbacks and scraper work
    can touch the DB from several ``asyncio.to_thread`` worker threads.  A
    single RLock plus WAL mode keeps that access deterministic and simple.
    """

    def __init__(
        self,
        path: str,
        *,
        monitored_users: Iterable[str],
        legacy_json_path: str | None = None,
        migrated_backup_path: str | None = None,
        backup_path: str | None = None,
    ):
        self.path = os.path.abspath(path)
        self.backup_path = os.path.abspath(backup_path) if backup_path else None
        self.legacy_json_path = legacy_json_path
        self.migrated_backup_path = migrated_backup_path
        self._lock = RLock()
        self._closed = False

        self.monitored_users = tuple(
            dict.fromkeys(
                str(user).strip()
                for user in monitored_users
                if str(user).strip()
            )
        )
        self._monitored_casefold = {
            user.casefold(): user
            for user in self.monitored_users
        }

        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._recover_corrupt_database_if_possible()

        self.connection = sqlite3.connect(
            self.path,
            timeout=30,
            check_same_thread=False,
        )
        self.connection.row_factory = sqlite3.Row

        self._configure()
        self._create_or_upgrade_schema()

        # Create the configured user rows before importing legacy ratings so
        # the ratings foreign key always has a valid parent.
        self.restrict_to_config_users()
        self._migrate_legacy_json_if_needed()
        self.restrict_to_config_users()

    # ------------------------------------------------------------------
    # Setup / recovery / schema
    # ------------------------------------------------------------------

    def _recover_corrupt_database_if_possible(self) -> None:
        if not os.path.exists(self.path) or os.path.getsize(self.path) == 0:
            return

        try:
            probe = sqlite3.connect(self.path, timeout=5)
            result = probe.execute("PRAGMA integrity_check").fetchone()
            probe.close()
            if result and result[0] == "ok":
                return
        except Exception:
            pass

        stamp = int(_now())
        corrupt_path = self.path + f".corrupt-{stamp}"
        shutil.move(self.path, corrupt_path)

        # WAL/SHM belong to the same database generation. Never let a stale
        # WAL attach itself to a restored backup.
        for suffix in ("-wal", "-shm"):
            sidecar = self.path + suffix
            if os.path.exists(sidecar):
                shutil.move(sidecar, corrupt_path + suffix)

        print(f"[DB] Wykryto uszkodzoną bazę; przeniesiono do {corrupt_path}.")

        if self.backup_path and os.path.exists(self.backup_path):
            try:
                probe = sqlite3.connect(self.backup_path, timeout=5)
                result = probe.execute("PRAGMA integrity_check").fetchone()
                probe.close()
                if result and result[0] == "ok":
                    shutil.copy2(self.backup_path, self.path)
                    print("[DB] Przywrócono lokalny backup SQLite.")
            except Exception as exc:
                print(f"[DB] Backup również nie przeszedł kontroli: {exc}")

    def _configure(self) -> None:
        with self._lock:
            cursor = self.connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA wal_autocheckpoint=1000")
            self.connection.commit()

    def _table_columns(self, table: str) -> set[str]:
        rows = self.connection.execute(
            f"PRAGMA table_info({table})"
        ).fetchall()
        return {str(row[1]) for row in rows}

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        if column in self._table_columns(table):
            return
        self.connection.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
        )

    def _create_or_upgrade_schema(self) -> None:
        with self._lock, self.connection:
            # Existing databases from the previous Kotone version already have
            # meta/users/ratings. CREATE IF NOT EXISTS keeps them intact.
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    username TEXT PRIMARY KEY COLLATE NOCASE,
                    format_monitor_version INTEGER
                )
                """
            )
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ratings (
                    username TEXT NOT NULL COLLATE NOCASE,
                    album_id TEXT NOT NULL,
                    score TEXT,
                    date TEXT,
                    artist TEXT,
                    album TEXT,
                    release_format TEXT,
                    has_review INTEGER NOT NULL DEFAULT 0,
                    has_track_ratings INTEGER NOT NULL DEFAULT 0,
                    liked INTEGER NOT NULL DEFAULT 0,
                    review_url TEXT,
                    PRIMARY KEY (username, album_id),
                    FOREIGN KEY (username)
                        REFERENCES users(username)
                        ON DELETE CASCADE
                        ON UPDATE CASCADE
                )
                """
            )

            # Profile columns (additive migration from old users table).
            user_columns = {
                "display_username": "TEXT",
                "profile_url": "TEXT",
                "avatar_url": "TEXT",
                "ratings_count": "TEXT",
                "reviews_count": "TEXT",
                "lists_count": "TEXT",
                "following_count": "TEXT",
                "followers_count": "TEXT",
                "average_rating": "REAL",
                "average_rating_text": "TEXT",
                "favorite_kind": "TEXT",
                "rating_distribution_json": "TEXT",
                "profile_json": "TEXT",
                "profile_synced_at": "REAL",
                "ratings_synced_at": "REAL",
                "full_ratings_synced_at": "REAL",
                "last_success_at": "REAL",
                "last_error": "TEXT",
                "last_error_at": "REAL",
                "created_at": "REAL",
                "updated_at": "REAL",
            }
            for column, definition in user_columns.items():
                self._ensure_column("users", column, definition)

            # Rich rating columns.
            rating_columns = {
                "sort_timestamp": "REAL",
                "artist_url": "TEXT",
                "album_url": "TEXT",
                "cover_url": "TEXT",
                "review_text": "TEXT",
                "detail_complete": "INTEGER NOT NULL DEFAULT 0",
                "detail_synced_at": "REAL",
                "first_seen_at": "REAL",
                "last_seen_at": "REAL",
                "active": "INTEGER NOT NULL DEFAULT 1",
                # Set only by a completed background archive after that format
                # has already been bootstrapped once. The monitor treats such
                # rows as new until a Discord notification is successfully sent.
                "notify_pending": "INTEGER NOT NULL DEFAULT 0",
            }
            for column, definition in rating_columns.items():
                self._ensure_column("ratings", column, definition)

            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS favorites (
                    username TEXT NOT NULL COLLATE NOCASE,
                    position INTEGER NOT NULL,
                    favorite_kind TEXT,
                    item_type TEXT,
                    name TEXT,
                    artist TEXT,
                    album TEXT,
                    url TEXT,
                    PRIMARY KEY (username, position),
                    FOREIGN KEY (username)
                        REFERENCES users(username)
                        ON DELETE CASCADE
                        ON UPDATE CASCADE
                )
                """
            )

            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS user_track_ratings (
                    username TEXT NOT NULL COLLATE NOCASE,
                    album_id TEXT NOT NULL,
                    track_key TEXT NOT NULL,
                    track_number INTEGER,
                    title TEXT,
                    score TEXT,
                    PRIMARY KEY (username, album_id, track_key),
                    FOREIGN KEY (username, album_id)
                        REFERENCES ratings(username, album_id)
                        ON DELETE CASCADE
                        ON UPDATE CASCADE
                )
                """
            )

            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS rating_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL COLLATE NOCASE,
                    album_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    old_score TEXT,
                    new_score TEXT,
                    changed_at REAL NOT NULL,
                    FOREIGN KEY (username)
                        REFERENCES users(username)
                        ON DELETE CASCADE
                        ON UPDATE CASCADE
                )
                """
            )

            # Per-format archive progress. This is deliberately separate from
            # notification monitoring: formats disabled for notifications are
            # still slowly archived for configured users.
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS rating_format_sync (
                    username TEXT NOT NULL COLLATE NOCASE,
                    format_key TEXT NOT NULL,
                    last_attempt_at REAL,
                    last_success_at REAL,
                    item_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    PRIMARY KEY (username, format_key),
                    FOREIGN KEY (username)
                        REFERENCES users(username)
                        ON DELETE CASCADE
                        ON UPDATE CASCADE
                )
                """
            )

            # Public release cache.  Rows are only allowed when at least one
            # configured user has that album in ratings.
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS releases (
                    album_id TEXT PRIMARY KEY,
                    artist TEXT,
                    artist_url TEXT,
                    album TEXT,
                    url TEXT,
                    cover_url TEXT,
                    user_score TEXT,
                    ratings_count TEXT,
                    release_date TEXT,
                    year TEXT,
                    album_format TEXT,
                    label TEXT,
                    labels_json TEXT,
                    genres_json TEXT,
                    secondary_genres_json TEXT,
                    vibes_json TEXT,
                    ranking_year TEXT,
                    year_ranking TEXT,
                    year_ranking_text TEXT,
                    fetched_at REAL NOT NULL
                )
                """
            )

            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS release_tracks (
                    album_id TEXT NOT NULL,
                    track_key TEXT NOT NULL,
                    track_number INTEGER,
                    title TEXT,
                    duration TEXT,
                    user_score TEXT,
                    disc TEXT,
                    url TEXT,
                    PRIMARY KEY (album_id, track_key),
                    FOREIGN KEY (album_id)
                        REFERENCES releases(album_id)
                        ON DELETE CASCADE
                )
                """
            )

            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_ratings_user_active "
                "ON ratings(username, active, sort_timestamp DESC)"
            )
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_ratings_album "
                "ON ratings(album_id)"
            )
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_history_user_time "
                "ON rating_history(username, changed_at DESC)"
            )

            self.connection.execute(
                """
                INSERT INTO meta(key, value)
                VALUES('schema_version', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (str(SCHEMA_VERSION),),
            )

    # ------------------------------------------------------------------
    # Scope: only config users are persistent
    # ------------------------------------------------------------------

    def canonical_username(self, username: str) -> str | None:
        return self._monitored_casefold.get(str(username or "").strip().casefold())

    def is_monitored(self, username: str) -> bool:
        return self.canonical_username(username) is not None

    def _require_monitored(self, username: str) -> str:
        canonical = self.canonical_username(username)
        if canonical is None:
            raise ValueError(
                f"Użytkownik {username!r} nie jest wpisany w config.json -> users; "
                "Kotone nie zapisze jego danych."
            )
        return canonical

    def restrict_to_config_users(self) -> None:
        """Delete persistent personal data for users removed from config."""

        with self._lock, self.connection:
            configured = {user.casefold() for user in self.monitored_users}
            existing = self.connection.execute(
                "SELECT username FROM users"
            ).fetchall()

            for row in existing:
                username = str(row["username"])
                if username.casefold() not in configured:
                    self.connection.execute(
                        "DELETE FROM users WHERE username = ?",
                        (username,),
                    )
                    print(
                        f"[DB] Usunięto dane {username}: użytkownik nie jest już w config."
                    )

            now = _now()
            for username in self.monitored_users:
                self.connection.execute(
                    """
                    INSERT INTO users(username, created_at, updated_at)
                    VALUES(?, ?, ?)
                    ON CONFLICT(username) DO UPDATE SET updated_at=excluded.updated_at
                    """,
                    (username, now, now),
                )

            self._cleanup_orphan_release_cache_locked()

    # ------------------------------------------------------------------
    # Legacy data.json migration
    # ------------------------------------------------------------------

    def _migrate_legacy_json_if_needed(self) -> None:
        if not self.legacy_json_path or not os.path.exists(self.legacy_json_path):
            return

        # If ratings already exist, SQLite is already the source of truth.
        row = self.connection.execute("SELECT 1 FROM ratings LIMIT 1").fetchone()
        if row is not None:
            return

        try:
            with open(self.legacy_json_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception as exc:
            print(f"[DB] Nie udało się odczytać starego data.json: {exc}")
            return

        users_payload = payload.get("users", {}) if isinstance(payload, dict) else {}
        if not isinstance(users_payload, dict):
            return

        imported_users = 0
        imported_ratings = 0

        with self._lock, self.connection:
            for raw_username, user_data in users_payload.items():
                canonical = self.canonical_username(raw_username)
                if canonical is None:
                    continue

                imported_users += 1
                user_data = user_data if isinstance(user_data, dict) else {}
                self.connection.execute(
                    """
                    UPDATE users
                    SET format_monitor_version = ?, updated_at = ?
                    WHERE username = ?
                    """,
                    (
                        user_data.get("format_monitor_version"),
                        _now(),
                        canonical,
                    ),
                )

                ratings = user_data.get("ratings", {})
                if not isinstance(ratings, dict):
                    continue

                for album_id, item in ratings.items():
                    item = item if isinstance(item, dict) else {}
                    item = dict(item)
                    item["album_id"] = str(album_id)
                    self._upsert_rating_locked(
                        canonical,
                        item,
                        record_history=False,
                        active=True,
                    )
                    imported_ratings += 1

        print(
            f"[DB] Migracja data.json -> SQLite: "
            f"{imported_users} użytkowników, {imported_ratings} ocen."
        )

        if self.migrated_backup_path:
            try:
                if os.path.exists(self.migrated_backup_path):
                    os.remove(self.migrated_backup_path)
                shutil.move(self.legacy_json_path, self.migrated_backup_path)
            except Exception as exc:
                print(f"[DB] Nie udało się przenieść data.json do backupu: {exc}")

    # ------------------------------------------------------------------
    # Profile data
    # ------------------------------------------------------------------

    def save_profile(self, username: str, profile: dict) -> None:
        username = self._require_monitored(username)
        profile = dict(profile or {})
        now = _now()

        favorites = list(profile.get("favorites") or [])
        distribution = dict(profile.get("rating_distribution") or {})

        # Store a structured snapshot too. Recent ratings live in their own
        # table and would unnecessarily duplicate data here.
        profile_snapshot = {
            key: value
            for key, value in profile.items()
            if key not in {"recent_ratings", "favorites", "favorite_albums", "favorite_artists"}
        }

        with self._lock, self.connection:
            self.connection.execute(
                """
                UPDATE users
                SET
                    display_username = ?,
                    profile_url = ?,
                    avatar_url = ?,
                    ratings_count = ?,
                    reviews_count = ?,
                    lists_count = ?,
                    following_count = ?,
                    followers_count = ?,
                    average_rating = ?,
                    average_rating_text = ?,
                    favorite_kind = ?,
                    rating_distribution_json = ?,
                    profile_json = ?,
                    profile_synced_at = ?,
                    last_success_at = ?,
                    last_error = NULL,
                    updated_at = ?
                WHERE username = ?
                """,
                (
                    profile.get("username") or username,
                    profile.get("url"),
                    profile.get("avatar"),
                    profile.get("ratings_count"),
                    profile.get("reviews_count"),
                    profile.get("lists_count"),
                    profile.get("following_count"),
                    profile.get("followers_count"),
                    profile.get("average_rating"),
                    profile.get("average_rating_text"),
                    profile.get("favorite_kind"),
                    _json_dump(distribution),
                    _json_dump(profile_snapshot),
                    now,
                    now,
                    now,
                    username,
                ),
            )

            self.connection.execute(
                "DELETE FROM favorites WHERE username = ?",
                (username,),
            )

            for position, item in enumerate(favorites):
                self.connection.execute(
                    """
                    INSERT INTO favorites(
                        username, position, favorite_kind, item_type,
                        name, artist, album, url
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        username,
                        position,
                        profile.get("favorite_kind"),
                        item.get("type"),
                        item.get("name"),
                        item.get("artist"),
                        item.get("album"),
                        item.get("url"),
                    ),
                )

    def get_profile(self, username: str, *, recent_limit: int = 50) -> dict | None:
        canonical = self.canonical_username(username)
        if canonical is None:
            return None

        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM users WHERE username = ?",
                (canonical,),
            ).fetchone()
            if row is None or row["profile_synced_at"] is None:
                return None

            favorites_rows = self.connection.execute(
                """
                SELECT * FROM favorites
                WHERE username = ?
                ORDER BY position
                """,
                (canonical,),
            ).fetchall()

        favorites = [
            {
                "type": r["item_type"],
                "name": r["name"],
                "artist": r["artist"],
                "album": r["album"],
                "url": r["url"],
            }
            for r in favorites_rows
        ]
        favorite_kind = row["favorite_kind"]

        return {
            "username": row["display_username"] or canonical,
            "url": row["profile_url"] or f"https://www.albumoftheyear.org/user/{canonical}/",
            "avatar": row["avatar_url"],
            "ratings_count": row["ratings_count"],
            "reviews_count": row["reviews_count"],
            "lists_count": row["lists_count"],
            "following_count": row["following_count"],
            "followers_count": row["followers_count"],
            "average_rating": row["average_rating"],
            "average_rating_text": row["average_rating_text"],
            "favorite_kind": favorite_kind,
            "favorites": favorites,
            "favorite_albums": favorites if favorite_kind == "albums" else [],
            "favorite_artists": favorites if favorite_kind == "artists" else [],
            "rating_distribution": _json_load(row["rating_distribution_json"], {}),
            "recent_ratings": self.get_recent_ratings(canonical, recent_limit),
            "profile_synced_at": row["profile_synced_at"],
            "ratings_synced_at": row["ratings_synced_at"],
        }

    def get_avatar(self, username: str) -> str | None:
        canonical = self.canonical_username(username)
        if canonical is None:
            return None
        with self._lock:
            row = self.connection.execute(
                "SELECT avatar_url FROM users WHERE username = ?",
                (canonical,),
            ).fetchone()
        return row["avatar_url"] if row else None

    # ------------------------------------------------------------------
    # Ratings
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_rating(row: sqlite3.Row) -> dict:
        return {
            "album_id": str(row["album_id"]),
            "score": row["score"],
            "date": row["date"],
            "artist": row["artist"],
            "artist_url": row["artist_url"],
            "album": row["album"],
            "url": row["album_url"],
            "cover": row["cover_url"],
            "release_format": row["release_format"],
            "sort_timestamp": row["sort_timestamp"],
            "has_review": bool(row["has_review"]),
            "has_track_ratings": bool(row["has_track_ratings"]),
            "liked": bool(row["liked"]),
            "review_url": row["review_url"],
            "review_text": row["review_text"],
            "detail_complete": bool(row["detail_complete"]),
            "detail_synced_at": row["detail_synced_at"],
            "active": bool(row["active"]),
            "notify_pending": bool(row["notify_pending"]),
        }

    def get_ratings_map(self, username: str, *, include_inactive: bool = False) -> dict[str, dict]:
        canonical = self.canonical_username(username)
        if canonical is None:
            return {}

        sql = "SELECT * FROM ratings WHERE username = ?"
        params: list = [canonical]
        if not include_inactive:
            sql += " AND active = 1"

        with self._lock:
            rows = self.connection.execute(sql, params).fetchall()

        return {
            str(row["album_id"]): self._row_to_rating(row)
            for row in rows
        }

    def get_recent_ratings(
        self,
        username: str,
        limit: int = 20,
        *,
        release_format: str | None = None,
    ) -> list[dict]:
        canonical = self.canonical_username(username)
        if canonical is None:
            return []

        try:
            limit = max(1, min(1000, int(limit)))
        except (TypeError, ValueError):
            limit = 20

        sql = "SELECT * FROM ratings WHERE username = ? AND active = 1"
        params: list = [canonical]

        if release_format:
            sql += " AND lower(COALESCE(release_format, '')) = lower(?)"
            params.append(release_format)

        sql += (
            " ORDER BY COALESCE(sort_timestamp, last_seen_at, first_seen_at, 0) DESC, "
            "rowid DESC LIMIT ?"
        )
        params.append(limit)

        with self._lock:
            rows = self.connection.execute(sql, params).fetchall()

        return [self._row_to_rating(row) for row in rows]

    def get_rating(self, username: str, album_id: str) -> dict | None:
        canonical = self.canonical_username(username)
        if canonical is None:
            return None

        with self._lock:
            row = self.connection.execute(
                """
                SELECT * FROM ratings
                WHERE username = ? AND album_id = ? AND active = 1
                """,
                (canonical, str(album_id)),
            ).fetchone()

        return self._row_to_rating(row) if row else None

    def _upsert_rating_locked(
        self,
        username: str,
        item: dict,
        *,
        record_history: bool,
        active: bool,
    ) -> tuple[bool, str | None]:
        album_id = str(item.get("album_id") or "").strip()
        if not album_id:
            raise ValueError("Rating bez album_id")

        existing = self.connection.execute(
            """
            SELECT score, active, first_seen_at
            FROM ratings
            WHERE username = ? AND album_id = ?
            """,
            (username, album_id),
        ).fetchone()

        old_score = str(existing["score"] or "") if existing else None
        new_score = str(item.get("score") or "")
        now = _now()
        is_new = existing is None

        first_seen = (
            existing["first_seen_at"]
            if existing and existing["first_seen_at"] is not None
            else now
        )

        self.connection.execute(
            """
            INSERT INTO ratings(
                username, album_id, score, date, sort_timestamp,
                artist, artist_url, album, album_url, cover_url,
                release_format, has_review, has_track_ratings, liked,
                review_url, first_seen_at, last_seen_at, active
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(username, album_id) DO UPDATE SET
                score = excluded.score,
                date = excluded.date,
                sort_timestamp = COALESCE(excluded.sort_timestamp, ratings.sort_timestamp),
                artist = COALESCE(excluded.artist, ratings.artist),
                artist_url = COALESCE(excluded.artist_url, ratings.artist_url),
                album = COALESCE(excluded.album, ratings.album),
                album_url = COALESCE(excluded.album_url, ratings.album_url),
                cover_url = COALESCE(excluded.cover_url, ratings.cover_url),
                release_format = COALESCE(excluded.release_format, ratings.release_format),
                has_review = excluded.has_review,
                has_track_ratings = excluded.has_track_ratings,
                liked = excluded.liked,
                review_url = COALESCE(excluded.review_url, ratings.review_url),
                last_seen_at = excluded.last_seen_at,
                active = excluded.active
            """,
            (
                username,
                album_id,
                new_score,
                item.get("date"),
                item.get("sort_timestamp"),
                item.get("artist"),
                item.get("artist_url"),
                item.get("album") or item.get("title"),
                item.get("url"),
                item.get("cover"),
                item.get("release_format") or item.get("album_format"),
                _bool_int(item.get("has_review")),
                _bool_int(item.get("has_track_ratings")),
                _bool_int(item.get("liked")),
                item.get("review_url"),
                first_seen,
                now,
                _bool_int(active),
            ),
        )

        if record_history:
            event_type = None
            if is_new:
                event_type = "new"
            elif old_score != new_score:
                event_type = "score"
            elif existing and not bool(existing["active"]) and active:
                event_type = "restored"

            if event_type:
                self.connection.execute(
                    """
                    INSERT INTO rating_history(
                        username, album_id, event_type,
                        old_score, new_score, changed_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (username, album_id, event_type, old_score, new_score, now),
                )

        return is_new, old_score

    def upsert_rating(
        self,
        username: str,
        item: dict,
        *,
        record_history: bool = False,
        active: bool = True,
    ) -> tuple[bool, str | None]:
        username = self._require_monitored(username)
        with self._lock, self.connection:
            result = self._upsert_rating_locked(
                username,
                item,
                record_history=record_history,
                active=active,
            )
            self.connection.execute(
                "UPDATE ratings SET notify_pending = 0 "
                "WHERE username = ? AND album_id = ?",
                (username, str(item.get("album_id") or "")),
            )
            self.connection.execute(
                "UPDATE users SET updated_at = ? WHERE username = ?",
                (_now(), username),
            )
            return result

    def upsert_ratings(
        self,
        username: str,
        ratings: Iterable[dict],
        *,
        record_history: bool = False,
        mark_missing_inactive: bool = False,
    ) -> None:
        username = self._require_monitored(username)
        ratings = list(ratings)
        seen_ids: set[str] = set()

        with self._lock, self.connection:
            for item in ratings:
                album_id = str(item.get("album_id") or "").strip()
                if not album_id:
                    continue
                seen_ids.add(album_id)
                self._upsert_rating_locked(
                    username,
                    item,
                    record_history=record_history,
                    active=True,
                )
                self.connection.execute(
                    "UPDATE ratings SET notify_pending = 0 "
                    "WHERE username = ? AND album_id = ?",
                    (username, album_id),
                )

            if mark_missing_inactive:
                current = self.connection.execute(
                    """
                    SELECT album_id, score FROM ratings
                    WHERE username = ? AND active = 1
                    """,
                    (username,),
                ).fetchall()

                for row in current:
                    album_id = str(row["album_id"])
                    if album_id in seen_ids:
                        continue
                    self.connection.execute(
                        """
                        UPDATE ratings
                        SET active = 0
                        WHERE username = ? AND album_id = ?
                        """,
                        (username, album_id),
                    )
                    if record_history:
                        self.connection.execute(
                            """
                            INSERT INTO rating_history(
                                username, album_id, event_type,
                                old_score, new_score, changed_at
                            ) VALUES(?, ?, 'removed', ?, NULL, ?)
                            """,
                            (username, album_id, row["score"], _now()),
                        )

            self.connection.execute(
                "UPDATE users SET updated_at = ? WHERE username = ?",
                (_now(), username),
            )
            self._cleanup_orphan_release_cache_locked()

    def mark_missing_inactive(
        self,
        username: str,
        live_album_ids: Iterable[str],
        *,
        record_history: bool = True,
    ) -> None:
        """Mark ratings absent from a *full* live sync as inactive.

        We keep the row/history instead of deleting it, so an AOTY un-rate and
        later re-rate can be represented without losing old information.
        """
        canonical = self._require_monitored(username)
        live_ids = {str(value) for value in live_album_ids}

        with self._lock, self.connection:
            rows = self.connection.execute(
                """
                SELECT album_id, score FROM ratings
                WHERE username = ? AND active = 1
                """,
                (canonical,),
            ).fetchall()

            for row in rows:
                album_id = str(row["album_id"])
                if album_id in live_ids:
                    continue

                self.connection.execute(
                    "UPDATE ratings SET active = 0 WHERE username = ? AND album_id = ?",
                    (canonical, album_id),
                )
                if record_history:
                    self.connection.execute(
                        """
                        INSERT INTO rating_history(
                            username, album_id, event_type, old_score, new_score, changed_at
                        ) VALUES(?, ?, 'removed', ?, NULL, ?)
                        """,
                        (canonical, album_id, row["score"], _now()),
                    )

            self._cleanup_orphan_release_cache_locked()

    def save_rating_detail(self, username: str, album_id: str, detail: dict) -> None:
        username = self._require_monitored(username)
        album_id = str(album_id)
        now = _now()
        track_ratings = list(detail.get("track_ratings") or [])

        with self._lock, self.connection:
            # If a detail was fetched before the rating row exists, ignore it.
            # This enforces the "only configured users" / monitored-data scope.
            exists = self.connection.execute(
                """
                SELECT 1 FROM ratings
                WHERE username = ? AND album_id = ?
                """,
                (username, album_id),
            ).fetchone()
            if exists is None:
                return

            self.connection.execute(
                """
                UPDATE ratings
                SET
                    score = COALESCE(?, score),
                    date = COALESCE(?, date),
                    has_review = ?,
                    has_track_ratings = ?,
                    liked = ?,
                    review_url = COALESCE(?, review_url),
                    review_text = ?,
                    detail_complete = ?,
                    detail_synced_at = ?
                WHERE username = ? AND album_id = ?
                """,
                (
                    detail.get("score"),
                    detail.get("date"),
                    _bool_int(detail.get("has_review")),
                    _bool_int(detail.get("has_track_ratings")),
                    _bool_int(detail.get("liked")),
                    detail.get("review_url"),
                    detail.get("review_text"),
                    _bool_int(not detail.get("detail_incomplete")),
                    now,
                    username,
                    album_id,
                ),
            )

            self.connection.execute(
                """
                DELETE FROM user_track_ratings
                WHERE username = ? AND album_id = ?
                """,
                (username, album_id),
            )

            for index, track in enumerate(track_ratings, start=1):
                number = track.get("number")
                title = str(track.get("title") or "").strip()
                key = f"{number if number is not None else 'x'}:{title.casefold()}:{index}"
                self.connection.execute(
                    """
                    INSERT INTO user_track_ratings(
                        username, album_id, track_key,
                        track_number, title, score
                    ) VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (
                        username,
                        album_id,
                        key,
                        number,
                        title or None,
                        track.get("score"),
                    ),
                )

    def get_rating_detail(self, username: str, album_id: str) -> dict | None:
        rating = self.get_rating(username, album_id)
        if rating is None:
            return None

        canonical = self._require_monitored(username)
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT track_number, title, score
                FROM user_track_ratings
                WHERE username = ? AND album_id = ?
                ORDER BY COALESCE(track_number, 99999), rowid
                """,
                (canonical, str(album_id)),
            ).fetchall()

        rating["track_ratings"] = [
            {
                "number": row["track_number"],
                "title": row["title"],
                "score": row["score"],
            }
            for row in rows
        ]
        rating["source"] = "SQLite cache"
        rating["detail_incomplete"] = not rating.get("detail_complete", False)
        return rating

    # ------------------------------------------------------------------
    # Release cache (only releases belonging to monitored-user ratings)
    # ------------------------------------------------------------------

    def _release_is_in_scope_locked(self, album_id: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM ratings WHERE album_id = ? LIMIT 1",
            (str(album_id),),
        ).fetchone()
        return row is not None

    def save_release_details(self, album_id: str, details: dict) -> bool:
        album_id = str(album_id or "").strip()
        if not album_id:
            return False

        details = dict(details or {})
        now = _now()

        with self._lock, self.connection:
            if not self._release_is_in_scope_locked(album_id):
                return False

            self.connection.execute(
                """
                INSERT INTO releases(
                    album_id, artist, artist_url, album, url, cover_url,
                    user_score, ratings_count, release_date, year,
                    album_format, label, labels_json, genres_json,
                    secondary_genres_json, vibes_json, ranking_year,
                    year_ranking, year_ranking_text, fetched_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(album_id) DO UPDATE SET
                    artist = excluded.artist,
                    artist_url = excluded.artist_url,
                    album = excluded.album,
                    url = excluded.url,
                    cover_url = excluded.cover_url,
                    user_score = excluded.user_score,
                    ratings_count = excluded.ratings_count,
                    release_date = excluded.release_date,
                    year = excluded.year,
                    album_format = excluded.album_format,
                    label = excluded.label,
                    labels_json = excluded.labels_json,
                    genres_json = excluded.genres_json,
                    secondary_genres_json = excluded.secondary_genres_json,
                    vibes_json = excluded.vibes_json,
                    ranking_year = excluded.ranking_year,
                    year_ranking = excluded.year_ranking,
                    year_ranking_text = excluded.year_ranking_text,
                    fetched_at = excluded.fetched_at
                """,
                (
                    album_id,
                    details.get("artist"),
                    details.get("artist_url"),
                    details.get("album"),
                    details.get("url"),
                    details.get("cover"),
                    details.get("user_score"),
                    details.get("ratings_count"),
                    details.get("release_date"),
                    details.get("year"),
                    details.get("album_format"),
                    details.get("label"),
                    _json_dump(list(details.get("labels") or [])),
                    _json_dump(list(details.get("genres") or [])),
                    _json_dump(list(details.get("secondary_genres") or [])),
                    _json_dump(list(details.get("vibes") or [])),
                    details.get("ranking_year"),
                    details.get("year_ranking"),
                    details.get("year_ranking_text"),
                    now,
                ),
            )

            self.connection.execute(
                "DELETE FROM release_tracks WHERE album_id = ?",
                (album_id,),
            )

            for index, track in enumerate(list(details.get("tracklist") or []), start=1):
                number = track.get("number")
                title = str(track.get("title") or "").strip()
                key = f"{number if number is not None else 'x'}:{title.casefold()}:{index}"
                self.connection.execute(
                    """
                    INSERT INTO release_tracks(
                        album_id, track_key, track_number, title,
                        duration, user_score, disc, url
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        album_id,
                        key,
                        number,
                        title or None,
                        track.get("duration"),
                        track.get("user_score"),
                        track.get("disc"),
                        track.get("url"),
                    ),
                )

        return True

    def get_release_details(self, album_id: str) -> dict | None:
        album_id = str(album_id or "").strip()
        if not album_id:
            return None

        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM releases WHERE album_id = ?",
                (album_id,),
            ).fetchone()
            if row is None:
                return None

            tracks = self.connection.execute(
                """
                SELECT track_number, title, duration, user_score, disc, url
                FROM release_tracks
                WHERE album_id = ?
                ORDER BY COALESCE(track_number, 99999), rowid
                """,
                (album_id,),
            ).fetchall()

        labels = _json_load(row["labels_json"], [])
        genres = _json_load(row["genres_json"], [])
        secondary = _json_load(row["secondary_genres_json"], [])
        vibes = _json_load(row["vibes_json"], [])

        return {
            "album_id": album_id,
            "artist": row["artist"],
            "artist_url": row["artist_url"],
            "album": row["album"],
            "url": row["url"],
            "cover": row["cover_url"],
            "user_score": row["user_score"],
            "ratings_count": row["ratings_count"],
            "release_date": row["release_date"],
            "year": row["year"],
            "album_format": row["album_format"],
            "label": row["label"],
            "labels": labels,
            "labels_text": ", ".join(labels) if labels else None,
            "genres": genres,
            "genres_text": ", ".join(genres) if genres else None,
            "secondary_genres": secondary,
            "secondary_genres_text": ", ".join(secondary) if secondary else None,
            "vibes": vibes,
            "vibes_text": ", ".join(vibes) if vibes else None,
            "ranking_year": row["ranking_year"],
            "year_ranking": row["year_ranking"],
            "year_ranking_text": row["year_ranking_text"],
            "tracklist": [
                {
                    "number": track["track_number"],
                    "title": track["title"],
                    "duration": track["duration"],
                    "user_score": track["user_score"],
                    "disc": track["disc"],
                    "url": track["url"],
                }
                for track in tracks
            ],
            "fetched_at": row["fetched_at"],
        }

    def _cleanup_orphan_release_cache_locked(self) -> None:
        self.connection.execute(
            """
            DELETE FROM releases
            WHERE album_id NOT IN (
                SELECT DISTINCT album_id FROM ratings
            )
            """
        )

    def detail_enrichment_candidates(self, username: str, limit: int) -> list[dict]:
        canonical = self.canonical_username(username)
        if canonical is None or limit <= 0:
            return []

        with self._lock:
            rows = self.connection.execute(
                """
                SELECT * FROM ratings
                WHERE username = ?
                  AND active = 1
                  AND (has_review = 1 OR has_track_ratings = 1)
                  AND detail_complete = 0
                ORDER BY COALESCE(sort_timestamp, first_seen_at, 0) DESC
                LIMIT ?
                """,
                (canonical, int(limit)),
            ).fetchall()
        return [self._row_to_rating(row) for row in rows]

    def release_enrichment_candidates(self, username: str, limit: int) -> list[dict]:
        canonical = self.canonical_username(username)
        if canonical is None or limit <= 0:
            return []

        with self._lock:
            rows = self.connection.execute(
                """
                SELECT r.*
                FROM ratings r
                LEFT JOIN releases rel ON rel.album_id = r.album_id
                WHERE r.username = ?
                  AND r.active = 1
                  AND rel.album_id IS NULL
                ORDER BY COALESCE(r.sort_timestamp, r.first_seen_at, 0) DESC
                LIMIT ?
                """,
                (canonical, int(limit)),
            ).fetchall()
        return [self._row_to_rating(row) for row in rows]

    # ------------------------------------------------------------------
    # Silent profile archive: every AOTY rating format
    # ------------------------------------------------------------------

    def archive_due_formats(
        self,
        username: str,
        format_keys: Iterable[str],
        *,
        interval: float,
        limit: int,
    ) -> list[str]:
        """Return oldest/due profile formats without touching AOTY.

        Missing rows are due first.  Successful formats become due again only
        after ``interval``.  A failed format remains due so a later cycle can
        retry it, but because the monitor processes only a small number per
        cycle it cannot create a retry storm.
        """
        canonical = self.canonical_username(username)
        if canonical is None or limit <= 0:
            return []

        keys = [str(key) for key in format_keys if str(key)]
        if not keys:
            return []

        with self._lock:
            rows = self.connection.execute(
                """
                SELECT format_key, last_success_at, last_attempt_at
                FROM rating_format_sync
                WHERE username = ?
                """,
                (canonical,),
            ).fetchall()

        status = {str(row["format_key"]): row for row in rows}
        now = _now()
        due = []

        for position, key in enumerate(keys):
            row = status.get(key)
            success_at = float(row["last_success_at"] or 0) if row else 0.0
            attempt_at = float(row["last_attempt_at"] or 0) if row else 0.0

            if success_at and now - success_at < max(0.0, float(interval)):
                continue

            # Never hammer a repeatedly failing route every few seconds. The
            # normal monitor cadence is much longer, but this guard also makes
            # manual/re-entrant calls safe.
            if not success_at and attempt_at and now - attempt_at < 5 * 60:
                continue

            due.append((success_at or -1.0, attempt_at or -1.0, position, key))

        due.sort(key=lambda item: (item[0], item[1], item[2]))
        return [item[3] for item in due[: int(limit)]]

    def mark_format_sync(
        self,
        username: str,
        format_key: str,
        *,
        success: bool,
        item_count: int = 0,
        error: str | None = None,
    ) -> None:
        canonical = self._require_monitored(username)
        now = _now()

        with self._lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO rating_format_sync(
                    username, format_key, last_attempt_at, last_success_at,
                    item_count, last_error
                ) VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(username, format_key) DO UPDATE SET
                    last_attempt_at = excluded.last_attempt_at,
                    last_success_at = CASE
                        WHEN excluded.last_success_at IS NOT NULL
                        THEN excluded.last_success_at
                        ELSE rating_format_sync.last_success_at
                    END,
                    item_count = CASE
                        WHEN excluded.last_success_at IS NOT NULL
                        THEN excluded.item_count
                        ELSE rating_format_sync.item_count
                    END,
                    last_error = excluded.last_error
                """,
                (
                    canonical,
                    str(format_key),
                    now,
                    now if success else None,
                    max(0, int(item_count)),
                    None if success else str(error or "unknown")[:1000],
                ),
            )

    def upsert_format_snapshot(
        self,
        username: str,
        release_format: str,
        ratings: Iterable[dict],
        *,
        preserve_existing_state: bool = False,
        deactivate_missing: bool = True,
        mark_new_pending: bool = False,
    ) -> None:
        """Persist one comprehensive AOTY format snapshot silently.

        ``preserve_existing_state`` is used for notification-enabled formats.
        Their older/missing ratings may be added to the archive, but an archive
        pass must not overwrite the saved score/active state *before* the
        monitor compares it and sends a change notification.

        Missing rows are deactivated only for snapshots that own that format
        and only when at least one row was parsed. An unexpected empty parser
        result can therefore never wipe a previously healthy cache.

        ``mark_new_pending`` is enabled only after a notification-enabled format
        has completed its first archive. A newly discovered row is then cached
        immediately but flagged for the monitor, preventing the background
        crawler from silently consuming a new-rating notification.
        """
        canonical = self._require_monitored(username)
        ratings = list(ratings)
        seen_ids: set[str] = set()

        with self._lock, self.connection:
            for raw_item in ratings:
                item = dict(raw_item or {})
                album_id = str(item.get("album_id") or "").strip()
                if not album_id:
                    continue
                seen_ids.add(album_id)

                active = True
                if preserve_existing_state:
                    existing = self.connection.execute(
                        """
                        SELECT score, active
                        FROM ratings
                        WHERE username = ? AND album_id = ?
                        """,
                        (canonical, album_id),
                    ).fetchone()
                    if existing is not None:
                        item["score"] = existing["score"]
                        active = bool(existing["active"])

                is_new, _ = self._upsert_rating_locked(
                    canonical,
                    item,
                    record_history=False,
                    active=active,
                )

                if is_new and mark_new_pending:
                    self.connection.execute(
                        """
                        UPDATE ratings
                        SET notify_pending = 1
                        WHERE username = ? AND album_id = ?
                        """,
                        (canonical, album_id),
                    )

            if deactivate_missing and seen_ids:
                rows = self.connection.execute(
                    """
                    SELECT album_id
                    FROM ratings
                    WHERE username = ? AND active = 1
                      AND lower(COALESCE(release_format, '')) = lower(?)
                    """,
                    (canonical, str(release_format)),
                ).fetchall()

                for row in rows:
                    album_id = str(row["album_id"])
                    if album_id in seen_ids:
                        continue
                    self.connection.execute(
                        """
                        UPDATE ratings
                        SET active = 0
                        WHERE username = ? AND album_id = ?
                        """,
                        (canonical, album_id),
                    )

            self.connection.execute(
                "UPDATE users SET updated_at = ? WHERE username = ?",
                (_now(), canonical),
            )
            self._cleanup_orphan_release_cache_locked()

    def archive_status(self, username: str) -> dict[str, dict]:
        canonical = self.canonical_username(username)
        if canonical is None:
            return {}
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT format_key, last_attempt_at, last_success_at,
                       item_count, last_error
                FROM rating_format_sync
                WHERE username = ?
                """,
                (canonical,),
            ).fetchall()
        return {str(row["format_key"]): dict(row) for row in rows}

    # ------------------------------------------------------------------
    # Sync metadata / history / diagnostics
    # ------------------------------------------------------------------

    def set_monitor_version(self, username: str, version: int) -> None:
        canonical = self._require_monitored(username)
        with self._lock, self.connection:
            self.connection.execute(
                "UPDATE users SET format_monitor_version = ?, updated_at = ? WHERE username = ?",
                (int(version), _now(), canonical),
            )

    def get_monitor_version(self, username: str) -> int | None:
        canonical = self.canonical_username(username)
        if canonical is None:
            return None
        with self._lock:
            row = self.connection.execute(
                "SELECT format_monitor_version FROM users WHERE username = ?",
                (canonical,),
            ).fetchone()
        return row["format_monitor_version"] if row else None

    def mark_sync_success(self, username: str, *, full: bool = False) -> None:
        canonical = self._require_monitored(username)
        now = _now()
        with self._lock, self.connection:
            if full:
                self.connection.execute(
                    """
                    UPDATE users
                    SET ratings_synced_at = ?, full_ratings_synced_at = ?,
                        last_success_at = ?, last_error = NULL, updated_at = ?
                    WHERE username = ?
                    """,
                    (now, now, now, now, canonical),
                )
            else:
                self.connection.execute(
                    """
                    UPDATE users
                    SET ratings_synced_at = ?, last_success_at = ?,
                        last_error = NULL, updated_at = ?
                    WHERE username = ?
                    """,
                    (now, now, now, canonical),
                )

    def mark_sync_error(self, username: str, message: str) -> None:
        canonical = self.canonical_username(username)
        if canonical is None:
            return
        now = _now()
        with self._lock, self.connection:
            self.connection.execute(
                """
                UPDATE users
                SET last_error = ?, last_error_at = ?, updated_at = ?
                WHERE username = ?
                """,
                (str(message)[:1000], now, now, canonical),
            )

    def sync_timestamps(self, username: str) -> dict:
        canonical = self.canonical_username(username)
        if canonical is None:
            return {}
        with self._lock:
            row = self.connection.execute(
                """
                SELECT profile_synced_at, ratings_synced_at,
                       full_ratings_synced_at, last_success_at,
                       last_error, last_error_at
                FROM users WHERE username = ?
                """,
                (canonical,),
            ).fetchone()
        return dict(row) if row else {}

    def record_score_change(
        self,
        username: str,
        album_id: str,
        old_score: str | None,
        new_score: str | None,
    ) -> None:
        canonical = self._require_monitored(username)
        with self._lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO rating_history(
                    username, album_id, event_type,
                    old_score, new_score, changed_at
                ) VALUES(?, ?, 'score', ?, ?, ?)
                """,
                (canonical, str(album_id), old_score, new_score, _now()),
            )

    def health(self) -> bool:
        try:
            with self._lock:
                row = self.connection.execute("SELECT 1").fetchone()
            return bool(row and row[0] == 1)
        except Exception:
            return False

    def checkpoint(self) -> None:
        if self._closed:
            return
        with self._lock:
            try:
                self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self.connection.commit()
            except Exception as exc:
                print(f"[DB] WAL checkpoint nie powiódł się: {exc}")

    def backup_if_due(self, *, force: bool = False) -> bool:
        if not self.backup_path or self._closed:
            return False

        if not force and os.path.exists(self.backup_path):
            age = _now() - os.path.getmtime(self.backup_path)
            if age < LOCAL_DATABASE_BACKUP_INTERVAL:
                return False

        temp_path = self.backup_path + ".tmp"
        os.makedirs(os.path.dirname(self.backup_path) or ".", exist_ok=True)
        if os.path.exists(temp_path):
            os.remove(temp_path)

        with self._lock:
            target = sqlite3.connect(temp_path)
            try:
                self.connection.backup(target)
                target.commit()
            finally:
                target.close()

        os.replace(temp_path, self.backup_path)
        print(f"[DB] Utworzono lokalny backup: {self.backup_path}")
        return True

    def summary(self) -> dict:
        with self._lock:
            users = self.connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            ratings = self.connection.execute(
                "SELECT COUNT(*) FROM ratings WHERE active = 1"
            ).fetchone()[0]
            releases = self.connection.execute("SELECT COUNT(*) FROM releases").fetchone()[0]
        return {
            "users": int(users),
            "ratings": int(ratings),
            "releases": int(releases),
            "path": self.path,
        }

    def diagnostics(self) -> dict:
        """Return safe, read-only database statistics for /dbstats.

        Keeping the SQL here makes the Discord command intentionally dumb.
        Future schema changes only need to be handled in this repository
        layer instead of being duplicated in commands/dbstats.py.
        """
        with self._lock:
            quick_check_row = self.connection.execute(
                "PRAGMA quick_check"
            ).fetchone()

            quick_check = (
                str(quick_check_row[0])
                if quick_check_row
                else "unknown"
            )

            schema_row = self.connection.execute(
                """
                SELECT value
                FROM meta
                WHERE key = 'schema_version'
                """
            ).fetchone()

            try:
                schema_version = int(
                    schema_row["value"]
                    if schema_row
                    else SCHEMA_VERSION
                )
            except (TypeError, ValueError):
                schema_version = SCHEMA_VERSION

            def scalar(query: str, params=()) -> int:
                row = self.connection.execute(
                    query,
                    params,
                ).fetchone()

                if row is None or row[0] is None:
                    return 0

                return int(row[0])

            counts = {
                "users": scalar(
                    "SELECT COUNT(*) FROM users"
                ),
                "ratings_active": scalar(
                    "SELECT COUNT(*) FROM ratings WHERE active = 1"
                ),
                "ratings_total": scalar(
                    "SELECT COUNT(*) FROM ratings"
                ),
                "reviews": scalar(
                    """
                    SELECT COUNT(*)
                    FROM ratings
                    WHERE has_review = 1
                       OR NULLIF(TRIM(COALESCE(review_text, '')), '') IS NOT NULL
                    """
                ),
                "review_texts_cached": scalar(
                    """
                    SELECT COUNT(*)
                    FROM ratings
                    WHERE NULLIF(TRIM(COALESCE(review_text, '')), '') IS NOT NULL
                    """
                ),
                "track_rating_albums": scalar(
                    """
                    SELECT COUNT(*)
                    FROM ratings
                    WHERE has_track_ratings = 1
                    """
                ),
                "user_track_ratings": scalar(
                    "SELECT COUNT(*) FROM user_track_ratings"
                ),
                "favorites": scalar(
                    "SELECT COUNT(*) FROM favorites"
                ),
                "history": scalar(
                    "SELECT COUNT(*) FROM rating_history"
                ),
                "notify_pending": scalar(
                    "SELECT COUNT(*) FROM ratings WHERE notify_pending = 1"
                ),
                "releases": scalar(
                    "SELECT COUNT(*) FROM releases"
                ),
                "release_tracks": scalar(
                    "SELECT COUNT(*) FROM release_tracks"
                ),
                "format_sync_rows": scalar(
                    "SELECT COUNT(*) FROM rating_format_sync"
                ),
            }

            users = []

            for configured_username in self.monitored_users:
                row = self.connection.execute(
                    """
                    SELECT
                        username,
                        ratings_count,
                        reviews_count,
                        lists_count,
                        following_count,
                        followers_count,
                        profile_synced_at,
                        ratings_synced_at,
                        full_ratings_synced_at,
                        last_success_at,
                        last_error,
                        last_error_at
                    FROM users
                    WHERE username = ?
                    """,
                    (configured_username,),
                ).fetchone()

                if row is None:
                    continue

                username = str(row["username"])

                user_stats = {
                    "username": username,
                    "profile_ratings_count": row["ratings_count"],
                    "profile_reviews_count": row["reviews_count"],
                    "profile_lists_count": row["lists_count"],
                    "following_count": row["following_count"],
                    "followers_count": row["followers_count"],
                    "profile_synced_at": row["profile_synced_at"],
                    "ratings_synced_at": row["ratings_synced_at"],
                    "full_ratings_synced_at": row["full_ratings_synced_at"],
                    "last_success_at": row["last_success_at"],
                    "last_error": row["last_error"],
                    "last_error_at": row["last_error_at"],
                    "ratings_active": scalar(
                        """
                        SELECT COUNT(*)
                        FROM ratings
                        WHERE username = ?
                          AND active = 1
                        """,
                        (username,),
                    ),
                    "ratings_total": scalar(
                        """
                        SELECT COUNT(*)
                        FROM ratings
                        WHERE username = ?
                        """,
                        (username,),
                    ),
                    "notify_pending": scalar(
                        """
                        SELECT COUNT(*)
                        FROM ratings
                        WHERE username = ?
                          AND notify_pending = 1
                        """,
                        (username,),
                    ),
                    "reviews": scalar(
                        """
                        SELECT COUNT(*)
                        FROM ratings
                        WHERE username = ?
                          AND (
                              has_review = 1
                              OR NULLIF(
                                  TRIM(COALESCE(review_text, '')),
                                  ''
                              ) IS NOT NULL
                          )
                        """,
                        (username,),
                    ),
                    "track_rating_albums": scalar(
                        """
                        SELECT COUNT(*)
                        FROM ratings
                        WHERE username = ?
                          AND has_track_ratings = 1
                        """,
                        (username,),
                    ),
                    "track_rating_rows": scalar(
                        """
                        SELECT COUNT(*)
                        FROM user_track_ratings
                        WHERE username = ?
                        """,
                        (username,),
                    ),
                    "favorites": scalar(
                        """
                        SELECT COUNT(*)
                        FROM favorites
                        WHERE username = ?
                        """,
                        (username,),
                    ),
                    "archive_formats_seen": scalar(
                        """
                        SELECT COUNT(*)
                        FROM rating_format_sync
                        WHERE username = ?
                        """,
                        (username,),
                    ),
                    "archive_formats_ok": scalar(
                        """
                        SELECT COUNT(*)
                        FROM rating_format_sync
                        WHERE username = ?
                          AND last_success_at IS NOT NULL
                        """,
                        (username,),
                    ),
                    "archive_items": scalar(
                        """
                        SELECT COALESCE(SUM(item_count), 0)
                        FROM rating_format_sync
                        WHERE username = ?
                        """,
                        (username,),
                    ),
                    "archive_last_success_at": (
                        lambda archive_row: (
                            archive_row[0]
                            if archive_row is not None
                            else None
                        )
                    )(
                        self.connection.execute(
                            """
                            SELECT MAX(last_success_at)
                            FROM rating_format_sync
                            WHERE username = ?
                            """,
                            (username,),
                        ).fetchone()
                    ),
                    "archive_last_attempt_at": (
                        lambda archive_row: (
                            archive_row[0]
                            if archive_row is not None
                            else None
                        )
                    )(
                        self.connection.execute(
                            """
                            SELECT MAX(last_attempt_at)
                            FROM rating_format_sync
                            WHERE username = ?
                            """,
                            (username,),
                        ).fetchone()
                    ),
                }

                archive_error = self.connection.execute(
                    """
                    SELECT format_key, last_attempt_at, last_error
                    FROM rating_format_sync
                    WHERE username = ?
                      AND last_error IS NOT NULL
                    ORDER BY COALESCE(last_attempt_at, 0) DESC
                    LIMIT 1
                    """,
                    (username,),
                ).fetchone()

                user_stats["archive_error_format"] = (
                    archive_error["format_key"]
                    if archive_error is not None
                    else None
                )
                user_stats["archive_error_at"] = (
                    archive_error["last_attempt_at"]
                    if archive_error is not None
                    else None
                )
                user_stats["archive_error"] = (
                    archive_error["last_error"]
                    if archive_error is not None
                    else None
                )

                users.append(
                    user_stats
                )

            page_count = scalar(
                "PRAGMA page_count"
            )
            page_size = scalar(
                "PRAGMA page_size"
            )
            freelist_count = scalar(
                "PRAGMA freelist_count"
            )

        def file_size(path: str | None) -> int:
            if not path or not os.path.exists(path):
                return 0
            try:
                return int(
                    os.path.getsize(path)
                )
            except OSError:
                return 0

        database_size = file_size(
            self.path
        )
        wal_size = file_size(
            self.path + "-wal"
        )
        shm_size = file_size(
            self.path + "-shm"
        )
        backup_size = file_size(
            self.backup_path
        )

        backup_mtime = None

        if (
            self.backup_path
            and os.path.exists(self.backup_path)
        ):
            try:
                backup_mtime = float(
                    os.path.getmtime(
                        self.backup_path
                    )
                )
            except OSError:
                backup_mtime = None

        return {
            "healthy": quick_check.casefold() == "ok",
            "quick_check": quick_check,
            "schema_version": schema_version,
            "path": self.path,
            "backup_path": self.backup_path,
            "database_size": database_size,
            "wal_size": wal_size,
            "shm_size": shm_size,
            "disk_size": (
                database_size
                + wal_size
                + shm_size
            ),
            "backup_size": backup_size,
            "backup_mtime": backup_mtime,
            "page_count": page_count,
            "page_size": page_size,
            "freelist_count": freelist_count,
            "counts": counts,
            "users": users,
        }

    def close(self) -> None:
        if self._closed:
            return
        with self._lock:
            self.checkpoint()
            self.connection.close()
            self._closed = True


DB = Database(
    DATABASE_FILE,
    monitored_users=USERS,
    legacy_json_path=DATA_FILE,
    migrated_backup_path=MIGRATED_DATA_BACKUP_FILE,
    backup_path=DATABASE_BACKUP_FILE,
)
