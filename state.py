"""Persistent SQLite state used by the AOTY monitor.

The monitor still works with ``STORE.data`` exactly like older Kotone
versions, while durable state is stored in normalized SQLite tables.

On first launch after upgrading, an existing data.json is imported
automatically and renamed to data_migrated.json.bak.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from copy import deepcopy
from threading import RLock

from settings import (
    DATA_FILE,
    DATABASE_FILE,
    MIGRATED_DATA_BACKUP_FILE,
)

STATE_VERSION = 4


def create_empty_state() -> dict:
    return {
        "version": STATE_VERSION,
        "users": {},
    }


def _bool_int(value) -> int:
    return 1 if bool(value) else 0


class StateStore:
    """SQLite-backed state with the previous dict API preserved."""

    def __init__(
        self,
        database_path: str,
        legacy_json_path: str | None = None,
        migrated_backup_path: str | None = None,
    ):
        self.path = database_path
        self.legacy_json_path = legacy_json_path
        self.migrated_backup_path = migrated_backup_path
        self._lock = RLock()

        directory = os.path.dirname(
            os.path.abspath(self.path)
        )

        if directory:
            os.makedirs(
                directory,
                exist_ok=True,
            )

        self._connection = sqlite3.connect(
            self.path,
            timeout=30,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row

        self._configure_database()
        self._create_schema()
        self._migrate_legacy_json_if_needed()

        self.data = self._load()

    def _configure_database(self) -> None:
        cursor = self._connection.cursor()

        cursor.execute(
            "PRAGMA journal_mode=WAL"
        )
        cursor.execute(
            "PRAGMA synchronous=NORMAL"
        )
        cursor.execute(
            "PRAGMA foreign_keys=ON"
        )
        cursor.execute(
            "PRAGMA busy_timeout=30000"
        )

        self._connection.commit()

    def _create_schema(self) -> None:
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )

            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    username TEXT PRIMARY KEY COLLATE NOCASE,
                    format_monitor_version INTEGER
                )
                """
            )

            self._connection.execute(
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

            self._connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_ratings_username
                ON ratings(username)
                """
            )

            self._connection.execute(
                """
                INSERT INTO meta(key, value)
                VALUES('state_version', ?)
                ON CONFLICT(key)
                DO UPDATE SET value=excluded.value
                """,
                (
                    str(STATE_VERSION),
                ),
            )

    # --------------------------------------------------------
    # data.json -> SQLite
    # --------------------------------------------------------

    def _database_has_users(self) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM users LIMIT 1"
        ).fetchone()

        return row is not None

    def _load_legacy_json(self) -> dict | None:
        if (
            not self.legacy_json_path
            or not os.path.exists(
                self.legacy_json_path
            )
        ):
            return None

        try:
            with open(
                self.legacy_json_path,
                "r",
                encoding="utf-8",
            ) as file:
                loaded = json.load(file)

        except Exception as exc:
            print(
                f"[DATA] Nie udało się odczytać starego data.json: {exc}"
            )
            return None

        if (
            not isinstance(loaded, dict)
            or not isinstance(
                loaded.get("users"),
                dict,
            )
        ):
            print(
                "[DATA] Stary data.json ma nieprawidłową strukturę; "
                "pomijam migrację."
            )
            return None

        return loaded

    def _migrate_legacy_json_if_needed(self) -> None:
        if self._database_has_users():
            return

        legacy = self._load_legacy_json()

        if legacy is None:
            return

        # One transaction: either the whole JSON lands in SQLite, or none.
        self._write_state(
            legacy,
            delete_missing=True,
        )

        user_count = len(
            legacy.get("users", {})
        )

        rating_count = sum(
            len(
                user_data.get(
                    "ratings",
                    {},
                )
            )
            for user_data in legacy.get(
                "users",
                {},
            ).values()
            if isinstance(
                user_data,
                dict,
            )
        )

        print(
            f"[DATA] Migracja data.json -> SQLite zakończona: "
            f"{user_count} użytkowników, {rating_count} ocen."
        )

        # JSON is moved only after a successful SQLite transaction.
        if (
            self.legacy_json_path
            and self.migrated_backup_path
            and os.path.exists(
                self.legacy_json_path
            )
        ):
            try:
                if os.path.exists(
                    self.migrated_backup_path
                ):
                    os.remove(
                        self.migrated_backup_path
                    )

                shutil.move(
                    self.legacy_json_path,
                    self.migrated_backup_path,
                )

                print(
                    "[DATA] Stary data.json zapisano jako "
                    "data_migrated.json.bak."
                )

            except Exception as exc:
                print(
                    "[DATA] SQLite jest gotowe, ale nie udało się "
                    f"przenieść starego data.json do backupu: {exc}"
                )

    # --------------------------------------------------------
    # SQLite -> STORE.data
    # --------------------------------------------------------

    def _load(self) -> dict:
        state = create_empty_state()

        row = self._connection.execute(
            """
            SELECT value
            FROM meta
            WHERE key = 'state_version'
            """
        ).fetchone()

        if row is not None:
            try:
                state["version"] = int(
                    row["value"]
                )
            except (TypeError, ValueError):
                state["version"] = STATE_VERSION

        users = {}

        for row in self._connection.execute(
            """
            SELECT
                username,
                format_monitor_version
            FROM users
            ORDER BY username COLLATE NOCASE
            """
        ):
            user_data = {
                "ratings": {},
            }

            if row["format_monitor_version"] is not None:
                user_data[
                    "format_monitor_version"
                ] = int(
                    row[
                        "format_monitor_version"
                    ]
                )

            users[
                row["username"]
            ] = user_data

        for row in self._connection.execute(
            """
            SELECT
                username,
                album_id,
                score,
                date,
                artist,
                album,
                release_format,
                has_review,
                has_track_ratings,
                liked,
                review_url
            FROM ratings
            ORDER BY username COLLATE NOCASE, album_id
            """
        ):
            username = row[
                "username"
            ]

            user_data = users.setdefault(
                username,
                {
                    "ratings": {},
                },
            )

            rating = {
                "score": row["score"] or "",
                "date": row["date"],
                "artist": row["artist"],
                "album": row["album"],
            }

            if row["release_format"]:
                rating[
                    "release_format"
                ] = row[
                    "release_format"
                ]

            if row["has_review"]:
                rating[
                    "has_review"
                ] = True

            if row["has_track_ratings"]:
                rating[
                    "has_track_ratings"
                ] = True

            if row["liked"]:
                rating[
                    "liked"
                ] = True

            if row["review_url"]:
                rating[
                    "review_url"
                ] = row[
                    "review_url"
                ]

            user_data[
                "ratings"
            ][
                str(row["album_id"])
            ] = rating

        state["users"] = users
        return state

    # --------------------------------------------------------
    # STORE.data -> SQLite
    # --------------------------------------------------------

    def _write_state(
        self,
        state: dict,
        *,
        delete_missing: bool,
    ) -> None:
        users = (
            state.get("users", {})
            if isinstance(state, dict)
            else {}
        )

        if not isinstance(users, dict):
            users = {}

        try:
            state_version = int(
                state.get(
                    "version",
                    STATE_VERSION,
                )
            )
        except (TypeError, ValueError, AttributeError):
            state_version = STATE_VERSION

        with self._connection:
            self._connection.execute(
                """
                INSERT INTO meta(key, value)
                VALUES('state_version', ?)
                ON CONFLICT(key)
                DO UPDATE SET value=excluded.value
                """,
                (
                    str(state_version),
                ),
            )

            current_usernames = set()

            for username, user_data in users.items():
                username = str(
                    username
                ).strip()

                if not username:
                    continue

                current_usernames.add(
                    username.casefold()
                )

                if not isinstance(
                    user_data,
                    dict,
                ):
                    user_data = {}

                monitor_version = user_data.get(
                    "format_monitor_version"
                )

                try:
                    monitor_version = (
                        int(monitor_version)
                        if monitor_version is not None
                        else None
                    )
                except (TypeError, ValueError):
                    monitor_version = None

                self._connection.execute(
                    """
                    INSERT INTO users(
                        username,
                        format_monitor_version
                    )
                    VALUES(?, ?)
                    ON CONFLICT(username)
                    DO UPDATE SET
                        format_monitor_version =
                            excluded.format_monitor_version
                    """,
                    (
                        username,
                        monitor_version,
                    ),
                )

                ratings = user_data.get(
                    "ratings",
                    {},
                )

                if not isinstance(
                    ratings,
                    dict,
                ):
                    ratings = {}

                current_album_ids = set()

                for album_id, rating in ratings.items():
                    album_id = str(
                        album_id
                    ).strip()

                    if not album_id:
                        continue

                    current_album_ids.add(
                        album_id
                    )

                    if not isinstance(
                        rating,
                        dict,
                    ):
                        rating = {}

                    self._connection.execute(
                        """
                        INSERT INTO ratings(
                            username,
                            album_id,
                            score,
                            date,
                            artist,
                            album,
                            release_format,
                            has_review,
                            has_track_ratings,
                            liked,
                            review_url
                        )
                        VALUES(
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                        )
                        ON CONFLICT(username, album_id)
                        DO UPDATE SET
                            score = excluded.score,
                            date = excluded.date,
                            artist = excluded.artist,
                            album = excluded.album,
                            release_format = excluded.release_format,
                            has_review = excluded.has_review,
                            has_track_ratings = excluded.has_track_ratings,
                            liked = excluded.liked,
                            review_url = excluded.review_url
                        """,
                        (
                            username,
                            album_id,
                            str(
                                rating.get("score")
                                or ""
                            ),
                            rating.get("date"),
                            rating.get("artist"),
                            rating.get("album"),
                            rating.get("release_format"),
                            _bool_int(
                                rating.get(
                                    "has_review"
                                )
                            ),
                            _bool_int(
                                rating.get(
                                    "has_track_ratings"
                                )
                            ),
                            _bool_int(
                                rating.get(
                                    "liked"
                                )
                            ),
                            rating.get("review_url"),
                        ),
                    )

                if delete_missing:
                    existing = self._connection.execute(
                        """
                        SELECT album_id
                        FROM ratings
                        WHERE username = ?
                        """,
                        (
                            username,
                        ),
                    ).fetchall()

                    for row in existing:
                        existing_album_id = str(
                            row[
                                "album_id"
                            ]
                        )

                        if existing_album_id not in current_album_ids:
                            self._connection.execute(
                                """
                                DELETE FROM ratings
                                WHERE username = ?
                                  AND album_id = ?
                                """,
                                (
                                    username,
                                    existing_album_id,
                                ),
                            )

            if delete_missing:
                existing_users = self._connection.execute(
                    "SELECT username FROM users"
                ).fetchall()

                for row in existing_users:
                    existing_username = str(
                        row[
                            "username"
                        ]
                    )

                    if (
                        existing_username.casefold()
                        not in current_usernames
                    ):
                        self._connection.execute(
                            """
                            DELETE FROM users
                            WHERE username = ?
                            """,
                            (
                                existing_username,
                            ),
                        )

    def save(self) -> None:
        with self._lock:
            self._write_state(
                self.data,
                delete_missing=True,
            )

    def reload(self) -> dict:
        with self._lock:
            self.data = self._load()

            return deepcopy(
                self.data
            )

    def close(self) -> None:
        with self._lock:
            try:
                self._connection.commit()
            finally:
                self._connection.close()


STORE = StateStore(
    DATABASE_FILE,
    legacy_json_path=DATA_FILE,
    migrated_backup_path=MIGRATED_DATA_BACKUP_FILE,
)
