"""SQLite persistence layer for Kotone.

Only AOTY users explicitly listed in ``config.json -> users`` are persisted.
Commands may still *read* arbitrary public AOTY pages, but those users are not
written to this database.

The database is intentionally richer than the old data.json state:
- full profile summary + favorites + rating distribution;
- all monitored ratings and their flags;
- review / track-rating details once fetched;
- append-only change history for ratings, reviews, likes, Track Ratings, profile/Favorites;
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
from pathlib import Path
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

SCHEMA_VERSION = 10


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
        self.recovery_marker_path = self.path + ".recovery-required"
        self.legacy_json_path = legacy_json_path
        self.migrated_backup_path = migrated_backup_path
        self._lock = RLock()
        self._closed = False
        self.pre_migration_backup_path: str | None = None
        self.pre_prune_backup_path: str | None = None

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
        database_existed_before_open = (
            os.path.exists(self.path)
            and os.path.getsize(self.path) > 0
        )
        stored_schema_version = (
            self._stored_schema_version_from_file(self.path)
            if database_existed_before_open
            else None
        )
        if (
            stored_schema_version is not None
            and stored_schema_version > SCHEMA_VERSION
        ):
            raise RuntimeError(
                "Baza SQLite ma nowszy schema_version "
                f"({stored_schema_version}) niż obsługiwany przez tę wersję "
                f"Kotone ({SCHEMA_VERSION}). Przerwano start bez zmiany bazy."
            )

        self.connection = sqlite3.connect(
            self.path,
            timeout=30,
            check_same_thread=False,
        )
        self.connection.row_factory = sqlite3.Row

        try:
            self._configure()

            if (
                database_existed_before_open
                and (
                    stored_schema_version is None
                    or stored_schema_version < SCHEMA_VERSION
                )
            ):
                self._create_verified_pre_migration_backup(
                    stored_schema_version
                )

            self._create_or_upgrade_schema()

            # Create the configured user rows before importing legacy ratings so
            # the ratings foreign key always has a valid parent.
            self.restrict_to_config_users()
            self._migrate_legacy_json_if_needed()
            self.restrict_to_config_users()
        except Exception:
            try:
                self.connection.close()
            finally:
                self._closed = True
            raise

    # ------------------------------------------------------------------
    # Setup / recovery / schema
    # ------------------------------------------------------------------

    @staticmethod
    def _sqlite_error_is_corruption(exc: sqlite3.Error) -> bool:
        code = getattr(exc, "sqlite_errorcode", None)
        if code is not None:
            base_code = int(code) & 0xFF
            corruption_codes = {
                int(getattr(sqlite3, "SQLITE_CORRUPT", 11)),
                int(getattr(sqlite3, "SQLITE_NOTADB", 26)),
            }
            if base_code in corruption_codes:
                return True

        message = str(exc).casefold()
        return (
            "database disk image is malformed" in message
            or "file is not a database" in message
            or "malformed database schema" in message
        )

    @classmethod
    def _probe_database_file(cls, path: str) -> tuple[str, str | None]:
        """Return ``healthy``, ``missing``, ``corrupt`` or ``unavailable``.

        Operational failures (lock, permissions, I/O) must not be interpreted
        as corruption.  The read-only probe is always closed in ``finally`` so
        Windows can safely rename a genuinely corrupt database afterwards.
        """
        try:
            if not os.path.exists(path) or os.path.getsize(path) == 0:
                return "missing", None
        except OSError as exc:
            return "unavailable", f"{type(exc).__name__}: {exc}"

        probe: sqlite3.Connection | None = None
        try:
            uri = Path(os.path.abspath(path)).as_uri() + "?mode=ro"
            probe = sqlite3.connect(uri, uri=True, timeout=5)
            rows = probe.execute("PRAGMA integrity_check").fetchall()
            messages = [str(row[0]) for row in rows if row]
            if messages == ["ok"]:
                return "healthy", None
            detail = "; ".join(messages[:5]) or "integrity_check nie zwrócił wyniku"
            return "corrupt", detail
        except sqlite3.Error as exc:
            status = (
                "corrupt"
                if cls._sqlite_error_is_corruption(exc)
                else "unavailable"
            )
            return status, f"{type(exc).__name__}: {exc}"
        finally:
            if probe is not None:
                try:
                    probe.close()
                except sqlite3.Error:
                    pass

    def _quarantine_database_files(self, reason: str) -> str:
        quarantine_path = self.path + f".{reason}-{time.time_ns()}"
        if os.path.exists(self.path):
            shutil.move(self.path, quarantine_path)

        # WAL/SHM belong to the same database generation. Never let a stale
        # sidecar attach itself to a restored backup or a newly created DB.
        for suffix in ("-wal", "-shm"):
            sidecar = self.path + suffix
            if os.path.exists(sidecar):
                shutil.move(sidecar, quarantine_path + suffix)

        return quarantine_path

    def _mark_recovery_required(self, reason: str) -> None:
        """Persist fail-closed state across Railway restart attempts."""

        temp_path = self.recovery_marker_path + ".tmp"
        Path(temp_path).write_text(
            str(reason).strip() or "manual SQLite recovery required",
            encoding="utf-8",
        )
        os.replace(temp_path, self.recovery_marker_path)

    def _clear_recovery_marker(self) -> None:
        try:
            os.remove(self.recovery_marker_path)
        except FileNotFoundError:
            pass

    def _recover_corrupt_database_if_possible(self) -> None:
        recovery_was_required = os.path.exists(self.recovery_marker_path)
        main_file_existed = os.path.exists(self.path)
        main_status, main_detail = self._probe_database_file(self.path)
        if main_status == "healthy":
            # An operator may have restored a verified database while the bot
            # was stopped. A healthy main file is the only manual action that
            # clears the persistent fail-closed marker without using backup.
            if recovery_was_required:
                self._clear_recovery_marker()
            return
        if main_status == "unavailable":
            raise RuntimeError(
                "Nie można bezpiecznie sprawdzić bazy SQLite; plik nie został "
                f"uznany za uszkodzony ani przeniesiony: {main_detail}"
            )

        backup_status = "missing"
        backup_detail = None
        if self.backup_path:
            backup_status, backup_detail = self._probe_database_file(
                self.backup_path
            )
            if backup_status == "unavailable":
                raise RuntimeError(
                    "Nie można bezpiecznie sprawdzić lokalnego backupu SQLite; "
                    f"przerwano odzyskiwanie: {backup_detail}"
                )

        quarantine_path = None
        if (
            os.path.exists(self.path)
            or os.path.exists(self.path + "-wal")
            or os.path.exists(self.path + "-shm")
        ):
            reason = "corrupt" if main_status == "corrupt" else "empty"
            quarantine_path = self._quarantine_database_files(reason)

        if main_status == "corrupt":
            print(
                "[DB] Wykryto uszkodzoną bazę; "
                f"przeniesiono do {quarantine_path}. Szczegóły: {main_detail}"
            )

        if backup_status == "healthy" and self.backup_path:
            shutil.copy2(self.backup_path, self.path)
            restored_status, restored_detail = self._probe_database_file(self.path)
            if restored_status != "healthy":
                raise RuntimeError(
                    "Skopiowany backup SQLite nie przeszedł kontroli po "
                    f"odtworzeniu: {restored_detail or restored_status}"
                )
            self._clear_recovery_marker()
            print("[DB] Przywrócono lokalny backup SQLite.")
            return

        if backup_status == "corrupt":
            print(
                "[DB] Lokalny backup SQLite jest uszkodzony i nie został "
                f"przywrócony: {backup_detail}"
            )

        if main_status == "corrupt":
            self._mark_recovery_required(
                f"corrupt main quarantined at {quarantine_path}"
            )
            raise RuntimeError(
                "Uszkodzona baza SQLite została zachowana w kwarantannie "
                f"{quarantine_path}, ale nie ma poprawnego backupu. "
                "Przerwano start zamiast tworzyć pustą bazę."
            )

        if main_status == "missing" and backup_status == "corrupt":
            self._mark_recovery_required("main missing and backup corrupt")
            raise RuntimeError(
                "Główna baza SQLite nie istnieje lub ma 0 B, a dostępny "
                "backup jest uszkodzony. Przerwano start zamiast tworzyć "
                "pustą bazę."
            )

        if main_status == "missing" and (
            recovery_was_required or main_file_existed
        ):
            self._mark_recovery_required(
                "main database missing/empty without a healthy backup"
            )
            raise RuntimeError(
                "Główna baza SQLite jest pusta albo wymaga ręcznego "
                "odzyskania, a nie ma poprawnego backupu. Przerwano start "
                "zamiast tworzyć pustą bazę."
            )

    @classmethod
    def _stored_schema_version_from_file(cls, path: str) -> int | None:
        probe: sqlite3.Connection | None = None
        try:
            uri = Path(os.path.abspath(path)).as_uri() + "?mode=ro"
            probe = sqlite3.connect(uri, uri=True, timeout=5)
            meta_exists = probe.execute(
                """
                SELECT 1
                FROM sqlite_master
                WHERE type = 'table' AND name = 'meta'
                """
            ).fetchone()
            if meta_exists is None:
                return None

            row = probe.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                return None

            try:
                version = int(row[0])
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    "Nieprawidłowa wartość meta.schema_version w SQLite."
                ) from exc
            if version < 0:
                raise RuntimeError(
                    "meta.schema_version w SQLite nie może być ujemny."
                )
            return version
        finally:
            if probe is not None:
                probe.close()

    def _create_verified_pre_migration_backup(
        self,
        stored_schema_version: int | None,
    ) -> None:
        source_label = (
            f"v{stored_schema_version}"
            if stored_schema_version is not None
            else "legacy"
        )
        final_path = (
            f"{self.path}.pre-{source_label}-to-v{SCHEMA_VERSION}-"
            f"{time.time_ns()}.sqlite3"
        )
        self._create_verified_snapshot(
            final_path,
            purpose="przed migracją SQLite",
        )
        self.pre_migration_backup_path = final_path

    def _create_verified_snapshot(self, final_path: str, *, purpose: str) -> None:
        temp_path = final_path + ".tmp.sqlite3"

        try:
            target = sqlite3.connect(temp_path)
            try:
                with self._lock:
                    self.connection.backup(target)
                target.commit()
            finally:
                target.close()

            status, detail = self._probe_database_file(temp_path)
            if status != "healthy":
                raise RuntimeError(
                    f"Kopia bezpieczeństwa {purpose} nie przeszła kontroli: "
                    f"{detail or status}"
                )

            os.replace(temp_path, final_path)
        except Exception:
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except OSError:
                pass
            raise

        print(f"[DB] Zweryfikowana kopia {purpose}: {final_path}")

    def _create_verified_pre_prune_backup(
        self,
        removed_usernames: Iterable[str],
    ) -> None:
        final_path = (
            f"{self.path}.pre-config-prune-{time.time_ns()}.sqlite3"
        )
        self._create_verified_snapshot(
            final_path,
            purpose=(
                "przed usunięciem danych użytkowników spoza config "
                f"({', '.join(removed_usernames)})"
            ),
        )
        self.pre_prune_backup_path = final_path

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

    def _import_legacy_rating_history_locked(self) -> None:
        """Copy legacy score events using a restart/rollback-safe watermark.

        Existing Railway volumes may already contain rating_history entries.
        Older v10 builds stored only a boolean ``..._imported`` marker.  A later
        rollback to v9 could append new rows behind that marker, so every row
        above the numeric watermark is checked for an equivalent unified event
        before it is imported. This remains idempotent for old and new v10 DBs.
        """
        watermark_row = self.connection.execute(
            """
            SELECT value
            FROM meta
            WHERE key = 'change_history_legacy_watermark'
            """
        ).fetchone()
        try:
            watermark = max(0, int(watermark_row[0])) if watermark_row else 0
        except (TypeError, ValueError):
            watermark = 0

        rows = self.connection.execute(
            """
            SELECT id, username, album_id, event_type,
                   old_score, new_score, changed_at
            FROM rating_history
            WHERE id > ?
            ORDER BY id
            """,
            (watermark,),
        ).fetchall()

        event_map = {
            "new": "rating_added",
            "score": "score_changed",
            "removed": "rating_removed",
            "restored": "rating_restored",
        }

        for row in rows:
            event_type = event_map.get(str(row["event_type"]), "rating_changed")
            old_json = (
                _json_dump(row["old_score"])
                if row["old_score"] is not None
                else None
            )
            new_json = (
                _json_dump(row["new_score"])
                if row["new_score"] is not None
                else None
            )

            # Current v10 writes rating_history and change_history in one
            # transaction for backward compatibility. Such a row may be above
            # the last startup watermark but is already mirrored and must not
            # be duplicated. Exact timestamps/values are shared by both writes.
            mirrored = self.connection.execute(
                """
                SELECT 1
                FROM change_history
                WHERE username = ?
                  AND album_id = ?
                  AND entity_type = 'rating'
                  AND event_type = ?
                  AND field_name = 'score'
                  AND item_key IS NULL
                  AND old_value_json IS ?
                  AND new_value_json IS ?
                  AND detected_at = ?
                LIMIT 1
                """,
                (
                    row["username"],
                    row["album_id"],
                    event_type,
                    old_json,
                    new_json,
                    row["changed_at"],
                ),
            ).fetchone()

            if mirrored is None:
                self.connection.execute(
                    """
                    INSERT INTO change_history(
                        username, album_id, entity_type, event_type,
                        field_name, item_key, old_value_json,
                        new_value_json, source, detected_at
                    ) VALUES(?, ?, 'rating', ?, 'score', NULL, ?, ?, 'legacy', ?)
                    """,
                    (
                        row["username"],
                        row["album_id"],
                        event_type,
                        old_json,
                        new_json,
                        row["changed_at"],
                    ),
                )

            watermark = max(watermark, int(row["id"]))

        self.connection.execute(
            """
            INSERT INTO meta(key, value)
            VALUES('change_history_legacy_imported', '1')
            ON CONFLICT(key) DO UPDATE SET value='1'
            """
        )
        self.connection.execute(
            """
            INSERT INTO meta(key, value)
            VALUES('change_history_legacy_watermark', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (str(watermark),),
        )

    def _record_change_locked(
        self,
        username: str,
        *,
        entity_type: str,
        event_type: str,
        old_value=None,
        new_value=None,
        album_id: str | None = None,
        field_name: str | None = None,
        item_key: str | None = None,
        source: str = "unknown",
        detected_at: float | None = None,
    ) -> None:
        """Append one normalized change event inside the current transaction."""
        self.connection.execute(
            """
            INSERT INTO change_history(
                username, album_id, entity_type, event_type,
                field_name, item_key, old_value_json,
                new_value_json, source, detected_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                username,
                str(album_id) if album_id is not None else None,
                str(entity_type),
                str(event_type),
                field_name,
                item_key,
                _json_dump(old_value) if old_value is not None else None,
                _json_dump(new_value) if new_value is not None else None,
                str(source or "unknown")[:80],
                float(detected_at if detected_at is not None else _now()),
            ),
        )

    @staticmethod
    def _normalized_track_map(items: Iterable[dict]) -> dict[str, dict]:
        """Canonical scored-track map used for stable diffing.

        AOTY sometimes omits track numbers.  Number is preferred when present;
        otherwise normalized title is used. Unrated public tracks (NR/None) are
        ignored because they are not user Track Ratings.
        """
        result: dict[str, dict] = {}
        collisions: dict[str, int] = {}

        for raw in items:
            item = dict(raw or {})
            score = item.get("score")
            if score in (None, "", "NR", "N/R"):
                continue

            number = item.get("number")
            title = str(item.get("title") or "").strip()
            if number not in (None, ""):
                base = f"n:{number}"
            else:
                base = f"t:{title.casefold()}"

            count = collisions.get(base, 0) + 1
            collisions[base] = count
            key = base if count == 1 else f"{base}#{count}"
            result[key] = {
                "number": number,
                "title": title or None,
                "score": str(score),
            }

        return result

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

            # Unified, append-only audit log.  rating_history stays in place
            # for backward compatibility, while every new mutable user datum
            # (score, review, like, track rating, profile/favorites) can be
            # represented here without adding another one-off history table.
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS change_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL COLLATE NOCASE,
                    album_id TEXT,
                    entity_type TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    field_name TEXT,
                    item_key TEXT,
                    old_value_json TEXT,
                    new_value_json TEXT,
                    source TEXT NOT NULL DEFAULT 'unknown',
                    detected_at REAL NOT NULL,
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
                "CREATE INDEX IF NOT EXISTS idx_change_history_user_time "
                "ON change_history(username, detected_at DESC)"
            )
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_change_history_album_time "
                "ON change_history(username, album_id, detected_at DESC)"
            )

            self._import_legacy_rating_history_locked()

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

        with self._lock:
            configured = {user.casefold() for user in self.monitored_users}
            existing = self.connection.execute(
                "SELECT username FROM users"
            ).fetchall()
            removed_usernames = [
                str(row["username"])
                for row in existing
                if str(row["username"]).casefold() not in configured
            ]

            if removed_usernames:
                self._create_verified_pre_prune_backup(removed_usernames)

            with self.connection:
                for username in removed_usernames:
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
        """Persist one profile snapshot and append meaningful profile diffs.

        The first successful snapshot establishes a baseline and intentionally
        produces no history flood. Later snapshots record counters/average,
        distribution and Favorites changes in the unified change_history log.
        """
        username = self._require_monitored(username)
        profile = dict(profile or {})
        now = _now()

        favorites = list(profile.get("favorites") or [])
        distribution = dict(profile.get("rating_distribution") or {})

        profile_snapshot = {
            key: value
            for key, value in profile.items()
            if key not in {
                "recent_ratings",
                "favorites",
                "favorite_albums",
                "favorite_artists",
            }
        }

        with self._lock, self.connection:
            previous = self.connection.execute(
                "SELECT * FROM users WHERE username = ?",
                (username,),
            ).fetchone()
            had_baseline = bool(
                previous is not None
                and previous["profile_synced_at"] is not None
            )

            previous_favorite_rows = self.connection.execute(
                """
                SELECT item_type, name, artist, album, url
                FROM favorites
                WHERE username = ?
                ORDER BY position
                """,
                (username,),
            ).fetchall()
            previous_favorites = [dict(row) for row in previous_favorite_rows]

            # None from a temporarily incomplete parser must not erase a known
            # value. Explicit 0/"0" values are still stored normally.
            self.connection.execute(
                """
                UPDATE users
                SET
                    display_username = COALESCE(?, display_username),
                    profile_url = COALESCE(?, profile_url),
                    avatar_url = COALESCE(?, avatar_url),
                    ratings_count = COALESCE(?, ratings_count),
                    reviews_count = COALESCE(?, reviews_count),
                    lists_count = COALESCE(?, lists_count),
                    following_count = COALESCE(?, following_count),
                    followers_count = COALESCE(?, followers_count),
                    average_rating = COALESCE(?, average_rating),
                    average_rating_text = COALESCE(?, average_rating_text),
                    favorite_kind = COALESCE(?, favorite_kind),
                    rating_distribution_json = CASE
                        WHEN ? IS NOT NULL THEN ?
                        ELSE rating_distribution_json
                    END,
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
                    _json_dump(distribution) if distribution else None,
                    _json_dump(distribution) if distribution else None,
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

            normalized_favorites: list[dict] = []
            for position, item in enumerate(favorites):
                normalized = {
                    "item_type": item.get("type"),
                    "name": item.get("name"),
                    "artist": item.get("artist"),
                    "album": item.get("album"),
                    "url": item.get("url"),
                }
                normalized_favorites.append(normalized)
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
                        normalized["item_type"],
                        normalized["name"],
                        normalized["artist"],
                        normalized["album"],
                        normalized["url"],
                    ),
                )

            if not had_baseline or previous is None:
                return

            profile_fields = {
                "ratings_count": profile.get("ratings_count"),
                "reviews_count": profile.get("reviews_count"),
                "lists_count": profile.get("lists_count"),
                "following_count": profile.get("following_count"),
                "followers_count": profile.get("followers_count"),
                "average_rating": profile.get("average_rating"),
                "favorite_kind": profile.get("favorite_kind"),
            }

            for field_name, new_value in profile_fields.items():
                if new_value is None:
                    continue
                old_value = previous[field_name]
                if old_value == new_value:
                    continue
                self._record_change_locked(
                    username,
                    entity_type="profile",
                    event_type="profile_field_changed",
                    field_name=field_name,
                    old_value=old_value,
                    new_value=new_value,
                    source="profile_sync",
                    detected_at=now,
                )

            if distribution:
                old_distribution = _json_load(
                    previous["rating_distribution_json"],
                    {},
                )
                if old_distribution != distribution:
                    self._record_change_locked(
                        username,
                        entity_type="profile",
                        event_type="rating_distribution_changed",
                        field_name="rating_distribution",
                        old_value=old_distribution,
                        new_value=distribution,
                        source="profile_sync",
                        detected_at=now,
                    )

            if previous_favorites != normalized_favorites:
                self._record_change_locked(
                    username,
                    entity_type="favorites",
                    event_type="favorites_changed",
                    field_name="favorites",
                    old_value=previous_favorites,
                    new_value=normalized_favorites,
                    source="profile_sync",
                    detected_at=now,
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

    def get_rating_average(self, username: str) -> tuple[float | None, int]:
        """Return the exact mean of all persisted numeric scores for a user.

        This intentionally does not use AOTY's rating distribution and does
        not filter inactive historical rows: the profile UI promises the mean
        of ratings stored in Kotone's SQLite archive.
        """

        canonical = self.canonical_username(username)
        if canonical is None:
            return None, 0
        with self._lock:
            row = self.connection.execute(
                """
                SELECT AVG(CAST(score AS REAL)) AS average_score,
                       COUNT(score) AS score_count
                FROM ratings
                WHERE username = ?
                  AND TRIM(CAST(score AS TEXT)) <> ''
                  AND TRIM(CAST(score AS TEXT)) NOT GLOB '*[^0-9]*'
                """,
                (canonical,),
            ).fetchone()
        if row is None or not row["score_count"]:
            return None, 0
        return float(row["average_score"]), int(row["score_count"])

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

    def set_notify_pending(
        self,
        username: str,
        album_id: str,
        pending: bool = True,
    ) -> None:
        """Mark a cached new rating as still awaiting monitor delivery."""
        canonical = self._require_monitored(username)
        with self._lock, self.connection:
            self.connection.execute(
                """
                UPDATE ratings
                SET notify_pending = ?
                WHERE username = ? AND album_id = ?
                """,
                (_bool_int(pending), canonical, str(album_id)),
            )

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

    def cached_artists(self) -> list[dict]:
        """Artists already present in configured users' durable ratings.

        The database is intentionally the first autocomplete source.  This
        keeps artist lookup usable while AOTY is rate-limited or presenting a
        challenge page, without expanding the config-only persistence scope.
        """

        with self._lock:
            rows = self.connection.execute(
                """
                SELECT
                    artist,
                    MAX(NULLIF(TRIM(COALESCE(artist_url, '')), '')) AS artist_url,
                    COUNT(DISTINCT album_id) AS release_count
                FROM ratings
                WHERE active = 1
                  AND NULLIF(TRIM(COALESCE(artist, '')), '') IS NOT NULL
                GROUP BY artist COLLATE NOCASE
                ORDER BY artist COLLATE NOCASE
                """
            ).fetchall()

        return [
            {
                "name": row["artist"],
                "url": row["artist_url"],
                "release_count": int(row["release_count"] or 0),
            }
            for row in rows
        ]

    def cached_artist_releases(self, artist: str) -> list[dict]:
        """Distinct cached releases for an artist, enriched when possible."""

        artist = str(artist or "").strip()
        if not artist:
            return []

        with self._lock:
            rows = self.connection.execute(
                """
                SELECT *
                FROM (
                SELECT
                    r.album_id AS album_id,
                    COALESCE(
                        MAX(NULLIF(TRIM(COALESCE(rel.artist, '')), '')),
                        MAX(NULLIF(TRIM(COALESCE(r.artist, '')), ''))
                    ) AS artist,
                    COALESCE(
                        MAX(NULLIF(TRIM(COALESCE(rel.artist_url, '')), '')),
                        MAX(NULLIF(TRIM(COALESCE(r.artist_url, '')), ''))
                    ) AS artist_url,
                    COALESCE(
                        MAX(NULLIF(TRIM(COALESCE(rel.album, '')), '')),
                        MAX(NULLIF(TRIM(COALESCE(r.album, '')), ''))
                    ) AS album,
                    COALESCE(
                        MAX(NULLIF(TRIM(COALESCE(rel.url, '')), '')),
                        MAX(NULLIF(TRIM(COALESCE(r.album_url, '')), ''))
                    ) AS url,
                    COALESCE(
                        MAX(NULLIF(TRIM(COALESCE(rel.cover_url, '')), '')),
                        MAX(NULLIF(TRIM(COALESCE(r.cover_url, '')), ''))
                    ) AS cover,
                    MAX(rel.user_score) AS user_score,
                    MAX(rel.ratings_count) AS ratings_count,
                    MAX(rel.release_date) AS release_date,
                    MAX(rel.year) AS year,
                    COALESCE(
                        MAX(NULLIF(TRIM(COALESCE(rel.album_format, '')), '')),
                        MAX(NULLIF(TRIM(COALESCE(r.release_format, '')), ''))
                    ) AS album_format,
                    MAX(COALESCE(r.sort_timestamp, r.last_seen_at, r.first_seen_at, 0))
                        AS cache_order
                FROM ratings r
                LEFT JOIN releases rel ON rel.album_id = r.album_id
                WHERE r.active = 1
                  AND r.artist = ? COLLATE NOCASE
                GROUP BY r.album_id
                ) AS cached_release
                WHERE NULLIF(TRIM(COALESCE(cached_release.album, '')), '')
                    IS NOT NULL
                ORDER BY
                    CASE WHEN cached_release.year IS NULL THEN 1 ELSE 0 END,
                    cached_release.year DESC,
                    cached_release.cache_order DESC,
                    cached_release.album COLLATE NOCASE
                """,
                (artist,),
            ).fetchall()

        return [
            {
                "album_id": str(row["album_id"]),
                "title": row["album"],
                "album": row["album"],
                "artist": row["artist"],
                "artist_url": row["artist_url"],
                "url": (
                    row["url"]
                    or "https://www.albumoftheyear.org/album/"
                    f"{row['album_id']}/"
                ),
                "cover": row["cover"],
                "user_score": row["user_score"],
                "ratings_count": row["ratings_count"],
                "release_date": row["release_date"],
                "year": row["year"],
                "album_format": row["album_format"],
                "release_format": row["album_format"],
                "source": "SQLite cache",
            }
            for row in rows
        ]

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
        record_changes: bool = False,
        source: str = "sync",
        record_new_change: bool = True,
    ) -> tuple[bool, str | None]:
        """Insert/update one rating without losing richer detail state.

        Rating cards know score and coarse flags, while the dedicated user
        release page is authoritative for review text, like and Track Ratings.
        Once such a detail baseline exists, a changed card flag marks the row
        dirty instead of overwriting the trusted detail immediately. The
        background detail worker then confirms the change and records a diff.
        """
        album_id = str(item.get("album_id") or "").strip()
        if not album_id:
            raise ValueError("Rating bez album_id")

        existing = self.connection.execute(
            """
            SELECT score, active, first_seen_at, notify_pending,
                   has_review, has_track_ratings, liked,
                   detail_complete, detail_synced_at
            FROM ratings
            WHERE username = ? AND album_id = ?
            """,
            (username, album_id),
        ).fetchone()

        old_score = (
            str(existing["score"])
            if existing is not None and existing["score"] is not None
            else None
        )
        incoming_score = item.get("score")
        new_score = str(incoming_score) if incoming_score is not None else ""
        now = _now()
        is_new = existing is None

        first_seen = (
            existing["first_seen_at"]
            if existing and existing["first_seen_at"] is not None
            else now
        )

        incoming_flags = {
            "has_review": _bool_int(item.get("has_review")),
            "has_track_ratings": _bool_int(item.get("has_track_ratings")),
            "liked": _bool_int(item.get("liked")),
        }
        stored_flags = dict(incoming_flags)
        detail_dirty = False

        # If we already fetched a real user-release detail page, do not allow
        # a coarse card/parser result to destructively replace that state.
        # A mismatch merely schedules a detail recheck.
        if existing is not None and existing["detail_synced_at"] is not None:
            for field_name in incoming_flags:
                old_flag = int(existing[field_name] or 0)
                if old_flag != incoming_flags[field_name]:
                    detail_dirty = True
                    stored_flags[field_name] = old_flag

        # Before a rich detail baseline exists, the repeated rating-card state
        # is still useful evidence. First bootstrap never enables
        # record_changes, so this records only a later observed transition.
        # Once a detail baseline exists, the block above defers to the actual
        # user-release page instead and avoids card/parser false positives.
        if (
            existing is not None
            and existing["detail_synced_at"] is None
            and record_changes
        ):
            old_review = bool(existing["has_review"])
            new_review = bool(incoming_flags["has_review"])
            if old_review != new_review:
                self._record_change_locked(
                    username,
                    entity_type="review",
                    event_type="review_added" if new_review else "review_removed",
                    album_id=album_id,
                    field_name="has_review",
                    old_value=old_review,
                    new_value=new_review,
                    source=f"{source}_card",
                    detected_at=now,
                )

            old_like = bool(existing["liked"])
            new_like = bool(incoming_flags["liked"])
            if old_like != new_like:
                self._record_change_locked(
                    username,
                    entity_type="like",
                    event_type="like_added" if new_like else "like_removed",
                    album_id=album_id,
                    field_name="liked",
                    old_value=old_like,
                    new_value=new_like,
                    source=f"{source}_card",
                    detected_at=now,
                )

            old_tracks = bool(existing["has_track_ratings"])
            new_tracks = bool(incoming_flags["has_track_ratings"])
            if old_tracks != new_tracks:
                self._record_change_locked(
                    username,
                    entity_type="track_rating",
                    event_type=(
                        "track_ratings_added"
                        if new_tracks
                        else "track_ratings_removed"
                    ),
                    album_id=album_id,
                    field_name="has_track_ratings",
                    old_value=old_tracks,
                    new_value=new_tracks,
                    source=f"{source}_card",
                    detected_at=now,
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
                stored_flags["has_review"],
                stored_flags["has_track_ratings"],
                stored_flags["liked"],
                item.get("review_url"),
                first_seen,
                now,
                _bool_int(active),
            ),
        )

        if detail_dirty:
            self.connection.execute(
                """
                UPDATE ratings
                SET detail_complete = 0
                WHERE username = ? AND album_id = ?
                """,
                (username, album_id),
            )

        legacy_event = None
        unified_event = None

        pending_before = bool(existing and existing["notify_pending"])
        if (is_new and record_new_change) or (pending_before and record_new_change):
            legacy_event = "new"
            unified_event = "rating_added"
        elif old_score != new_score:
            legacy_event = "score"
            unified_event = "score_changed"
        elif existing and not bool(existing["active"]) and active:
            legacy_event = "restored"
            unified_event = "rating_restored"

        if record_history and legacy_event:
            self.connection.execute(
                """
                INSERT INTO rating_history(
                    username, album_id, event_type,
                    old_score, new_score, changed_at
                )
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    username,
                    album_id,
                    legacy_event,
                    old_score,
                    new_score,
                    now,
                ),
            )

        if record_changes and unified_event:
            self._record_change_locked(
                username,
                entity_type="rating",
                event_type=unified_event,
                album_id=album_id,
                field_name="score",
                old_value=old_score,
                new_value=new_score,
                source=source,
                detected_at=now,
            )

        return is_new, old_score

    def upsert_rating(
        self,
        username: str,
        item: dict,
        *,
        record_history: bool = False,
        active: bool = True,
        record_changes: bool | None = None,
        source: str = "sync",
    ) -> tuple[bool, str | None]:
        username = self._require_monitored(username)
        if record_changes is None:
            record_changes = bool(record_history)

        with self._lock, self.connection:
            result = self._upsert_rating_locked(
                username,
                item,
                record_history=record_history,
                active=active,
                record_changes=bool(record_changes),
                source=source,
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
        record_changes: bool | None = None,
        source: str = "sync",
    ) -> None:
        username = self._require_monitored(username)
        ratings = list(ratings)
        seen_ids: set[str] = set()
        if record_changes is None:
            record_changes = bool(record_history)

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
                    record_changes=bool(record_changes),
                    source=source,
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
                    changed_at = _now()
                    if record_history:
                        self.connection.execute(
                            """
                            INSERT INTO rating_history(
                                username, album_id, event_type,
                                old_score, new_score, changed_at
                            ) VALUES(?, ?, 'removed', ?, NULL, ?)
                            """,
                            (username, album_id, row["score"], changed_at),
                        )
                    if record_changes:
                        self._record_change_locked(
                            username,
                            entity_type="rating",
                            event_type="rating_removed",
                            album_id=album_id,
                            field_name="score",
                            old_value=row["score"],
                            new_value=None,
                            source=source,
                            detected_at=changed_at,
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
        record_changes: bool = True,
        source: str = "full_sync",
    ) -> None:
        """Mark ratings absent from a trusted full snapshot as inactive."""
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

                changed_at = _now()
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
                        (canonical, album_id, row["score"], changed_at),
                    )
                if record_changes:
                    self._record_change_locked(
                        canonical,
                        entity_type="rating",
                        event_type="rating_removed",
                        album_id=album_id,
                        field_name="score",
                        old_value=row["score"],
                        source=source,
                        detected_at=changed_at,
                    )

            self._cleanup_orphan_release_cache_locked()

    def save_rating_detail(
        self,
        username: str,
        album_id: str,
        detail: dict,
        *,
        record_changes: bool = True,
        source: str = "detail_sync",
    ) -> bool:
        """Persist a trusted user-release detail snapshot and diff it.

        Incomplete/partially parsed pages are explicitly non-destructive: they
        can mark the row dirty for retry, but never erase a known review, like
        or Track Ratings. A first complete fetch establishes the baseline.
        Every later complete fetch appends precise changes to change_history.

        The score itself is *not* updated here. The notification monitor / full
        rating archive owns score state so a background detail refresh can
        never swallow a Discord score-change notification.
        """
        username = self._require_monitored(username)
        album_id = str(album_id)
        detail = dict(detail or {})
        now = _now()
        track_ratings = list(detail.get("track_ratings") or [])

        with self._lock, self.connection:
            existing = self.connection.execute(
                """
                SELECT * FROM ratings
                WHERE username = ? AND album_id = ?
                """,
                (username, album_id),
            ).fetchone()
            if existing is None:
                return False

            old_track_rows = self.connection.execute(
                """
                SELECT track_number AS number, title, score
                FROM user_track_ratings
                WHERE username = ? AND album_id = ?
                ORDER BY COALESCE(track_number, 99999), rowid
                """,
                (username, album_id),
            ).fetchall()
            old_tracks = [dict(row) for row in old_track_rows]
            old_track_map = self._normalized_track_map(old_tracks)
            new_track_map = self._normalized_track_map(track_ratings)
            old_has_track_ratings = bool(existing["has_track_ratings"])

            # A card-level Track Ratings flag means at least one real user
            # score exists. Until the detail parser returns such a score, an
            # empty/NR-only snapshot is not authoritative enough to clear the
            # flag or complete the row. The next background pass will retry.
            unverified_track_claim = bool(
                not new_track_map
                and (
                    detail.get("has_track_ratings")
                    or (old_has_track_ratings and not old_track_map)
                )
            )

            # A rate-limit/interstitial/parser hiccup must never turn a known
            # review/like/track set into an empty one.
            if detail.get("detail_incomplete") or unverified_track_claim:
                self.connection.execute(
                    """
                    UPDATE ratings
                    SET
                        date = COALESCE(?, date),
                        review_url = COALESCE(?, review_url),
                        detail_complete = 0,
                        last_seen_at = COALESCE(last_seen_at, ?)
                    WHERE username = ? AND album_id = ?
                    """,
                    (
                        detail.get("date"),
                        detail.get("review_url"),
                        now,
                        username,
                        album_id,
                    ),
                )
                return False

            review_text_raw = detail.get("review_text")
            review_text = (
                str(review_text_raw).strip()
                if review_text_raw not in (None, "")
                else None
            )

            new_has_review = bool(detail.get("has_review") or review_text)
            new_has_track_ratings = bool(
                detail.get("has_track_ratings")
                or new_track_map
            )
            new_liked = bool(detail.get("liked"))
            had_baseline = existing["detail_synced_at"] is not None

            old_review_text = (
                str(existing["review_text"]).strip()
                if existing["review_text"] not in (None, "")
                else None
            )
            old_has_review = bool(existing["has_review"])
            old_liked = bool(existing["liked"])

            self.connection.execute(
                """
                UPDATE ratings
                SET
                    date = COALESCE(?, date),
                    has_review = ?,
                    has_track_ratings = ?,
                    liked = ?,
                    review_url = COALESCE(?, review_url),
                    review_text = ?,
                    detail_complete = 1,
                    detail_synced_at = ?,
                    last_seen_at = COALESCE(last_seen_at, ?)
                WHERE username = ? AND album_id = ?
                """,
                (
                    detail.get("date"),
                    _bool_int(new_has_review),
                    _bool_int(new_has_track_ratings),
                    _bool_int(new_liked),
                    detail.get("review_url"),
                    review_text,
                    now,
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

            if had_baseline and record_changes:
                if old_has_review != new_has_review:
                    self._record_change_locked(
                        username,
                        entity_type="review",
                        event_type=(
                            "review_added"
                            if new_has_review
                            else "review_removed"
                        ),
                        album_id=album_id,
                        field_name="review",
                        old_value=old_review_text,
                        new_value=review_text,
                        source=source,
                        detected_at=now,
                    )
                elif old_review_text != review_text:
                    self._record_change_locked(
                        username,
                        entity_type="review",
                        event_type="review_edited",
                        album_id=album_id,
                        field_name="review",
                        old_value=old_review_text,
                        new_value=review_text,
                        source=source,
                        detected_at=now,
                    )

                if old_liked != new_liked:
                    self._record_change_locked(
                        username,
                        entity_type="like",
                        event_type="like_added" if new_liked else "like_removed",
                        album_id=album_id,
                        field_name="liked",
                        old_value=old_liked,
                        new_value=new_liked,
                        source=source,
                        detected_at=now,
                    )

                old_map = old_track_map
                new_map = new_track_map
                for track_key in sorted(set(old_map) | set(new_map)):
                    old_track = old_map.get(track_key)
                    new_track = new_map.get(track_key)
                    if old_track == new_track:
                        continue

                    if old_track is None:
                        event_type = "track_rating_added"
                    elif new_track is None:
                        event_type = "track_rating_removed"
                    else:
                        event_type = "track_rating_changed"

                    display_track = new_track or old_track or {}
                    number = display_track.get("number")
                    title = display_track.get("title") or "Unknown track"
                    item_key = (
                        f"{number}. {title}"
                        if number not in (None, "")
                        else str(title)
                    )

                    self._record_change_locked(
                        username,
                        entity_type="track_rating",
                        event_type=event_type,
                        album_id=album_id,
                        field_name="track_score",
                        item_key=item_key,
                        old_value=old_track,
                        new_value=new_track,
                        source=source,
                        detected_at=now,
                    )

                # The broad flag can change even if the parser could not map
                # a concrete scored track. Preserve that fact too.
                if (
                    old_has_track_ratings != new_has_track_ratings
                    and self._normalized_track_map(old_tracks)
                    == self._normalized_track_map(track_ratings)
                ):
                    self._record_change_locked(
                        username,
                        entity_type="track_rating",
                        event_type=(
                            "track_ratings_added"
                            if new_has_track_ratings
                            else "track_ratings_removed"
                        ),
                        album_id=album_id,
                        field_name="has_track_ratings",
                        old_value=old_has_track_ratings,
                        new_value=new_has_track_ratings,
                        source=source,
                        detected_at=now,
                    )

            self.connection.execute(
                "UPDATE users SET updated_at = ? WHERE username = ?",
                (now, username),
            )

        return True

    def get_rating_detail(self, username: str, album_id: str) -> dict | None:
        rating = self.get_rating(username, album_id)
        if rating is None:
            return None

        rating["track_ratings"] = self.get_user_track_ratings(
            username,
            album_id,
        )
        rating["source"] = "SQLite cache"
        rating["detail_incomplete"] = not rating.get("detail_complete", False)
        return rating

    def get_user_track_ratings(self, username: str, album_id: str) -> list[dict]:
        """Return stored personal track scores even if the rating card is stale.

        The tracklist UI must be able to render durable ``user_track_ratings``
        directly; it should not lose them merely because a compact rating card
        is temporarily incomplete or no longer present in the current list.
        """

        canonical = self.canonical_username(username)
        if canonical is None:
            return []
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

        return [
            {
                "number": row["track_number"],
                "title": row["title"],
                "score": row["score"],
            }
            for row in rows
        ]

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

        raw_section_complete = details.get("_section_complete")
        legacy_authoritative = not isinstance(raw_section_complete, dict)

        def section_complete(name: str) -> bool:
            # Older callers and test fixtures predate the parser contract and
            # remain fully authoritative. Parser-produced snapshots opt into
            # section-by-section non-destructive merging.
            return legacy_authoritative or bool(raw_section_complete.get(name))

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
                    artist = COALESCE(excluded.artist, releases.artist),
                    artist_url = COALESCE(excluded.artist_url, releases.artist_url),
                    album = COALESCE(excluded.album, releases.album),
                    url = COALESCE(excluded.url, releases.url),
                    cover_url = COALESCE(excluded.cover_url, releases.cover_url),
                    user_score = CASE WHEN ? THEN
                        COALESCE(excluded.user_score, releases.user_score)
                        ELSE releases.user_score END,
                    ratings_count = CASE WHEN ? THEN
                        COALESCE(excluded.ratings_count, releases.ratings_count)
                        ELSE releases.ratings_count END,
                    release_date = CASE WHEN ? THEN
                        COALESCE(excluded.release_date, releases.release_date)
                        ELSE releases.release_date END,
                    year = CASE WHEN ? THEN
                        COALESCE(excluded.year, releases.year)
                        ELSE releases.year END,
                    album_format = CASE WHEN ? THEN
                        COALESCE(excluded.album_format, releases.album_format)
                        ELSE releases.album_format END,
                    label = CASE WHEN ? THEN excluded.label ELSE releases.label END,
                    labels_json = CASE WHEN ? THEN
                        excluded.labels_json ELSE releases.labels_json END,
                    genres_json = CASE WHEN ? THEN
                        excluded.genres_json ELSE releases.genres_json END,
                    secondary_genres_json = CASE WHEN ? THEN
                        excluded.secondary_genres_json
                        ELSE releases.secondary_genres_json END,
                    vibes_json = CASE WHEN ? THEN
                        excluded.vibes_json ELSE releases.vibes_json END,
                    ranking_year = CASE WHEN ? THEN
                        excluded.ranking_year ELSE releases.ranking_year END,
                    year_ranking = CASE WHEN ? THEN
                        excluded.year_ranking ELSE releases.year_ranking END,
                    year_ranking_text = CASE WHEN ? THEN
                        excluded.year_ranking_text
                        ELSE releases.year_ranking_text END,
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
                    section_complete("score"),
                    section_complete("score"),
                    section_complete("release_date"),
                    section_complete("release_date"),
                    section_complete("format"),
                    section_complete("labels"),
                    section_complete("labels"),
                    section_complete("genres"),
                    section_complete("genres"),
                    section_complete("vibes"),
                    section_complete("ranking"),
                    section_complete("ranking"),
                    section_complete("ranking"),
                ),
            )

            if section_complete("tracklist"):
                self.connection.execute(
                    "DELETE FROM release_tracks WHERE album_id = ?",
                    (album_id,),
                )

                for index, track in enumerate(
                    list(details.get("tracklist") or []),
                    start=1,
                ):
                    number = track.get("number")
                    title = str(track.get("title") or "").strip()
                    key = (
                        f"{number if number is not None else 'x'}:"
                        f"{title.casefold()}:{index}"
                    )
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

    def detail_enrichment_candidates(
        self,
        username: str,
        limit: int,
        *,
        stale_before: float | None = None,
    ) -> list[dict]:
        """Rows whose user-release detail should be fetched/rechecked.

        Dirty/incomplete rows always come first. Successfully enriched rows are
        revisited only when they are old *and* contain mutable detail worth
        tracking (review, like, Track Ratings, or cached detail residue). This
        catches edits/removals without crawling every plain rating forever.
        """
        canonical = self.canonical_username(username)
        if canonical is None or limit <= 0:
            return []

        if stale_before is None:
            stale_before = -1.0

        with self._lock:
            rows = self.connection.execute(
                """
                SELECT r.*
                FROM ratings r
                WHERE r.username = ?
                  AND r.active = 1
                  AND (
                        (
                            r.has_track_ratings = 1
                            AND NOT EXISTS (
                                SELECT 1
                                FROM user_track_ratings utr_missing
                                WHERE utr_missing.username = r.username
                                  AND utr_missing.album_id = r.album_id
                                  AND NULLIF(
                                      TRIM(COALESCE(utr_missing.score, '')),
                                      ''
                                  ) IS NOT NULL
                                  AND UPPER(TRIM(utr_missing.score))
                                      NOT IN ('NR', 'N/R')
                            )
                        )
                        OR
                        r.detail_complete = 0
                        AND (
                            r.detail_synced_at IS NOT NULL
                            OR r.has_review = 1
                            OR r.has_track_ratings = 1
                            OR r.liked = 1
                            OR NULLIF(TRIM(COALESCE(r.review_text, '')), '') IS NOT NULL
                            OR EXISTS (
                                SELECT 1
                                FROM user_track_ratings utr
                                WHERE utr.username = r.username
                                  AND utr.album_id = r.album_id
                            )
                        )
                        OR (
                            r.detail_synced_at IS NOT NULL
                            AND r.detail_synced_at <= ?
                            AND (
                                r.has_review = 1
                                OR r.has_track_ratings = 1
                                OR r.liked = 1
                                OR NULLIF(TRIM(COALESCE(r.review_text, '')), '') IS NOT NULL
                                OR EXISTS (
                                    SELECT 1
                                    FROM user_track_ratings utr2
                                    WHERE utr2.username = r.username
                                      AND utr2.album_id = r.album_id
                                )
                            )
                        )
                  )
                ORDER BY
                    CASE
                        WHEN r.has_track_ratings = 1
                         AND NOT EXISTS (
                            SELECT 1
                            FROM user_track_ratings utr_priority
                            WHERE utr_priority.username = r.username
                              AND utr_priority.album_id = r.album_id
                              AND NULLIF(
                                  TRIM(COALESCE(utr_priority.score, '')),
                                  ''
                              ) IS NOT NULL
                              AND UPPER(TRIM(utr_priority.score))
                                  NOT IN ('NR', 'N/R')
                         ) THEN 0
                        WHEN r.detail_complete = 0 THEN 1
                        ELSE 2
                    END,
                    COALESCE(r.detail_synced_at, 0) ASC,
                    COALESCE(r.sort_timestamp, r.first_seen_at, 0) DESC
                LIMIT ?
                """,
                (canonical, float(stale_before), int(limit)),
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
                  AND NULLIF(TRIM(COALESCE(r.album_url, '')), '') IS NOT NULL
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
        record_history: bool = False,
        record_changes: bool = False,
        source: str = "archive",
    ) -> None:
        """Persist one comprehensive AOTY format snapshot safely.

        A successful non-empty full-format snapshot owns membership for that
        format, so missing rows may be marked inactive. Empty parses remain
        non-destructive. Notification-enabled formats can preserve score/active
        until the monitor announces a new/changed rating, while review/like/
        Track Rating flag mismatches merely dirty the richer detail cache.
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
                    record_history=record_history,
                    active=active,
                    record_changes=record_changes,
                    source=source,
                    # A newly discovered monitored-format row is announced by
                    # the monitor first. Do not create its "added" history in
                    # the low-priority archive before Discord delivery.
                    record_new_change=not mark_new_pending,
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

            # An unexpected empty page/parser result must never wipe a format.
            if deactivate_missing and seen_ids:
                rows = self.connection.execute(
                    """
                    SELECT album_id, score
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

                    changed_at = _now()
                    self.connection.execute(
                        """
                        UPDATE ratings
                        SET active = 0, notify_pending = 0
                        WHERE username = ? AND album_id = ?
                        """,
                        (canonical, album_id),
                    )

                    if record_history:
                        self.connection.execute(
                            """
                            INSERT INTO rating_history(
                                username, album_id, event_type,
                                old_score, new_score, changed_at
                            ) VALUES(?, ?, 'removed', ?, NULL, ?)
                            """,
                            (canonical, album_id, row["score"], changed_at),
                        )

                    if record_changes:
                        self._record_change_locked(
                            canonical,
                            entity_type="rating",
                            event_type="rating_removed",
                            album_id=album_id,
                            field_name="score",
                            old_value=row["score"],
                            new_value=None,
                            source=source,
                            detected_at=changed_at,
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
        changed_at = _now()
        with self._lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO rating_history(
                    username, album_id, event_type,
                    old_score, new_score, changed_at
                ) VALUES(?, ?, 'score', ?, ?, ?)
                """,
                (canonical, str(album_id), old_score, new_score, changed_at),
            )
            self._record_change_locked(
                canonical,
                entity_type="rating",
                event_type="score_changed",
                album_id=str(album_id),
                field_name="score",
                old_value=old_score,
                new_value=new_score,
                source="manual",
                detected_at=changed_at,
            )

    def get_change_history(
        self,
        username: str,
        *,
        limit: int = 20,
        category: str = "all",
        album_id: str | None = None,
    ) -> list[dict]:
        """Read newest unified change events for /history."""
        canonical = self.canonical_username(username)
        if canonical is None:
            return []

        limit = max(1, min(100, int(limit)))
        category = str(category or "all").strip().casefold()
        category_map = {
            "ratings": ("rating",),
            "reviews": ("review",),
            "likes": ("like",),
            "tracks": ("track_rating",),
            "profile": ("profile", "favorites"),
            "favorites": ("favorites",),
        }

        sql = """
            SELECT
                ch.*,
                r.artist,
                r.album,
                r.album_url
            FROM change_history ch
            LEFT JOIN ratings r
              ON r.username = ch.username
             AND r.album_id = ch.album_id
            WHERE ch.username = ?
        """
        params: list = [canonical]

        if category in category_map:
            types = category_map[category]
            placeholders = ",".join("?" for _ in types)
            sql += f" AND ch.entity_type IN ({placeholders})"
            params.extend(types)

        if album_id:
            sql += " AND ch.album_id = ?"
            params.append(str(album_id))

        sql += " ORDER BY ch.detected_at DESC, ch.id DESC LIMIT ?"
        params.append(limit)

        with self._lock:
            rows = self.connection.execute(sql, params).fetchall()

        result = []
        for row in rows:
            item = dict(row)
            item["old_value"] = _json_load(item.pop("old_value_json"), None)
            item["new_value"] = _json_load(item.pop("new_value_json"), None)
            result.append(item)
        return result

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
            foreign_key_violations = len(
                self.connection.execute(
                    "PRAGMA foreign_key_check"
                ).fetchall()
            )

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
                "likes": scalar(
                    "SELECT COUNT(*) FROM ratings WHERE liked = 1"
                ),
                "detail_complete": scalar(
                    "SELECT COUNT(*) FROM ratings WHERE detail_complete = 1"
                ),
                "detail_dirty": scalar(
                    """
                    SELECT COUNT(*)
                    FROM ratings
                    WHERE active = 1
                      AND detail_complete = 0
                      AND detail_synced_at IS NOT NULL
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
                    """
                    SELECT COUNT(*)
                    FROM user_track_ratings
                    WHERE NULLIF(TRIM(COALESCE(score, '')), '') IS NOT NULL
                      AND UPPER(TRIM(score)) NOT IN ('NR', 'N/R')
                    """
                ),
                "favorites": scalar(
                    "SELECT COUNT(*) FROM favorites"
                ),
                "history": scalar(
                    "SELECT COUNT(*) FROM change_history"
                ),
                "legacy_rating_history": scalar(
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
                    "likes": scalar(
                        """
                        SELECT COUNT(*)
                        FROM ratings
                        WHERE username = ?
                          AND liked = 1
                        """,
                        (username,),
                    ),
                    "detail_complete": scalar(
                        """
                        SELECT COUNT(*)
                        FROM ratings
                        WHERE username = ?
                          AND detail_complete = 1
                        """,
                        (username,),
                    ),
                    "detail_dirty": scalar(
                        """
                        SELECT COUNT(*)
                        FROM ratings
                        WHERE username = ?
                          AND active = 1
                          AND detail_complete = 0
                          AND detail_synced_at IS NOT NULL
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
                          AND NULLIF(
                              TRIM(COALESCE(score, '')),
                              ''
                          ) IS NOT NULL
                          AND UPPER(TRIM(score)) NOT IN ('NR', 'N/R')
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
                    "changes": scalar(
                        """
                        SELECT COUNT(*)
                        FROM change_history
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
            "healthy": (
                quick_check.casefold() == "ok"
                and foreign_key_violations == 0
            ),
            "quick_check": quick_check,
            "foreign_key_violations": foreign_key_violations,
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
