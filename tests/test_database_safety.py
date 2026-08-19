"""Focused regression tests for SQLite startup and migration safety.

This module intentionally imports no AOTY/Discord parser dependencies, so the
storage safety suite can run even in a minimal Python environment.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
TEST_RUNTIME = tempfile.mkdtemp(prefix="kotone-db-safety-runtime-")
os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ["DATA_DIR"] = TEST_RUNTIME
sys.path.insert(0, str(ROOT))

from database import Database, SCHEMA_VERSION  # noqa: E402
from settings import _validate_users  # noqa: E402


class SettingsSafetyTests(unittest.TestCase):
    def test_users_allow_list_is_strictly_validated(self):
        invalid_values = (
            None,
            [],
            "enso",
            [""],
            ["enso", 123],
            ["Enso", "enso"],
        )

        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(RuntimeError):
                    _validate_users(value)

        self.assertEqual(
            _validate_users([" enso ", "second-user"]),
            ["enso", "second-user"],
        )


class ArtifactHygieneTests(unittest.TestCase):
    def test_recovery_and_temporary_database_artifacts_are_ignored(self):
        patterns = {
            line.strip()
            for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        self.assertTrue(
            {
                "*.corrupt-*",
                "*.empty-*",
                "*.sqlite3.tmp",
                "*.tmp.sqlite3",
            }.issubset(patterns)
        )


class DatabaseStartupSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="kotone-db-safety-"))
        self.path = self.tmp / "kotone.sqlite3"
        self.backup = self.tmp / "kotone.backup.sqlite3"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_db(
        self,
        *,
        path: Path | None = None,
        users=("enso",),
    ) -> Database:
        return Database(
            str(path or self.path),
            monitored_users=users,
            backup_path=str(self.backup),
        )

    def seed_backup(self) -> None:
        db = self.make_db()
        db.upsert_rating(
            "enso",
            {
                "album_id": "safe-1",
                "score": "99",
                "artist": "A",
                "album": "Safe",
            },
        )
        self.assertTrue(db.backup_if_due(force=True))
        db.close()

    def assert_backup_was_restored(self) -> None:
        recovered = self.make_db()
        try:
            self.assertEqual(
                recovered.get_rating("enso", "safe-1")["score"],
                "99",
            )
        finally:
            recovered.close()

    def test_missing_main_database_restores_verified_backup(self):
        self.seed_backup()
        self.path.unlink()
        Path(str(self.path) + "-wal").write_bytes(b"stale-wal")
        Path(str(self.path) + "-shm").write_bytes(b"stale-shm")

        self.assert_backup_was_restored()
        self.assertFalse(Path(str(self.path) + "-wal").exists())
        self.assertFalse(Path(str(self.path) + "-shm").exists())

    def test_zero_byte_main_database_restores_verified_backup(self):
        self.seed_backup()
        self.path.write_bytes(b"")

        self.assert_backup_was_restored()
        self.assertTrue(list(self.tmp.glob("kotone.sqlite3.empty-*")))

    def test_corrupt_database_probe_is_closed_before_windows_quarantine(self):
        self.seed_backup()
        self.path.write_bytes(b"not a sqlite database")

        self.assert_backup_was_restored()
        self.assertTrue(list(self.tmp.glob("kotone.sqlite3.corrupt-*")))

    def test_corrupt_database_without_healthy_backup_fails_closed(self):
        db = self.make_db()
        db.upsert_rating("enso", {"album_id": "only-copy", "score": "88"})
        db.close()
        self.path.write_bytes(b"not a sqlite database")

        with self.assertRaisesRegex(RuntimeError, "zamiast tworzyć pustą bazę"):
            self.make_db()

        quarantined = list(self.tmp.glob("kotone.sqlite3.corrupt-*"))
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].read_bytes(), b"not a sqlite database")
        self.assertFalse(self.path.exists())

        marker = Path(str(self.path) + ".recovery-required")
        self.assertTrue(marker.is_file())

        # Railway restartPolicy=ON_FAILURE must not turn the second attempt
        # into a fresh empty database after the corrupt main was quarantined.
        with self.assertRaisesRegex(RuntimeError, "zamiast tworzyć pustą bazę"):
            self.make_db()
        self.assertFalse(self.path.exists())

    def test_zero_byte_database_without_backup_fails_closed(self):
        self.path.write_bytes(b"")

        with self.assertRaisesRegex(RuntimeError, "zamiast tworzyć pustą bazę"):
            self.make_db()

        self.assertFalse(self.path.exists())
        self.assertTrue(
            Path(str(self.path) + ".recovery-required").is_file()
        )

    def test_missing_main_with_corrupt_backup_fails_closed(self):
        self.seed_backup()
        self.path.unlink()
        self.backup.write_bytes(b"not a sqlite database")

        with self.assertRaisesRegex(RuntimeError, "backup jest uszkodzony"):
            self.make_db()

        self.assertFalse(self.path.exists())
        self.assertEqual(self.backup.read_bytes(), b"not a sqlite database")

    def test_operational_probe_error_does_not_quarantine_database(self):
        db = self.make_db()
        db.close()

        with mock.patch.object(
            Database,
            "_probe_database_file",
            return_value=("unavailable", "database is locked"),
        ):
            with self.assertRaises(RuntimeError):
                self.make_db()

        self.assertTrue(self.path.exists())
        self.assertFalse(list(self.tmp.glob("kotone.sqlite3.corrupt-*")))
        self.assertFalse(
            Database._sqlite_error_is_corruption(
                sqlite3.OperationalError("database is locked")
            )
        )
        self.assertTrue(
            Database._sqlite_error_is_corruption(
                sqlite3.DatabaseError("file is not a database")
            )
        )

    def test_future_schema_is_rejected_without_mutation(self):
        connection = sqlite3.connect(self.path)
        connection.execute(
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', '999')"
        )
        connection.commit()
        connection.close()
        before = self.path.read_bytes()

        with self.assertRaisesRegex(RuntimeError, "nowszy schema_version"):
            self.make_db()
        self.assertEqual(self.path.read_bytes(), before)

        check = sqlite3.connect(self.path)
        try:
            self.assertEqual(
                check.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone()[0],
                "999",
            )
            tables = {
                row[0]
                for row in check.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertEqual(tables, {"meta"})
        finally:
            check.close()

    def test_older_schema_gets_verified_pre_migration_copy(self):
        db = self.make_db()
        db.close()

        connection = sqlite3.connect(self.path)
        connection.execute(
            "UPDATE meta SET value='9' WHERE key='schema_version'"
        )
        connection.commit()
        connection.close()

        migrated = self.make_db()
        try:
            snapshot = Path(migrated.pre_migration_backup_path or "")
            self.assertTrue(snapshot.is_file())
            status, detail = Database._probe_database_file(str(snapshot))
            self.assertEqual((status, detail), ("healthy", None))

            old = sqlite3.connect(snapshot)
            try:
                self.assertEqual(
                    old.execute(
                        "SELECT value FROM meta WHERE key='schema_version'"
                    ).fetchone()[0],
                    "9",
                )
            finally:
                old.close()

            self.assertEqual(
                migrated.connection.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone()[0],
                str(SCHEMA_VERSION),
            )
        finally:
            migrated.close()

    def test_legacy_must_hear_seed_is_moved_into_sqlite(self):
        """Historical flags live in ``releases`` after the v16 migration."""

        db = self.make_db()
        db.upsert_rating(
            "enso",
            {"album_id": "104775", "artist": "A", "album": "Moon"},
        )
        db.connection.execute(
            "UPDATE meta SET value='15' WHERE key='schema_version'"
        )
        db.connection.commit()
        db.close()

        migrated = self.make_db()
        try:
            details = migrated.get_release_details("104775")
            self.assertIsNotNone(details)
            self.assertTrue(details["must_hear"])
        finally:
            migrated.close()

    def test_config_allow_list_change_snapshots_before_prune_once(self):
        db = self.make_db(users=("enso",))
        db.upsert_rating(
            "enso",
            {"album_id": "kept-in-snapshot", "score": "97"},
        )
        db.close()

        changed = self.make_db(users=("ens0",))
        try:
            snapshot = Path(changed.pre_prune_backup_path or "")
            self.assertTrue(snapshot.is_file())
            self.assertEqual(
                Database._probe_database_file(str(snapshot)),
                ("healthy", None),
            )
            self.assertEqual(changed.summary()["users"], 1)
            self.assertEqual(changed.summary()["ratings"], 0)

            archived = sqlite3.connect(snapshot)
            try:
                self.assertEqual(
                    archived.execute(
                        "SELECT COUNT(*) FROM users WHERE username='enso'"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    archived.execute(
                        """
                        SELECT COUNT(*) FROM ratings
                        WHERE username='enso' AND album_id='kept-in-snapshot'
                        """
                    ).fetchone()[0],
                    1,
                )
            finally:
                archived.close()
        finally:
            changed.close()

        unchanged = self.make_db(users=("ens0",))
        try:
            self.assertIsNone(unchanged.pre_prune_backup_path)
        finally:
            unchanged.close()

    def test_legacy_history_watermark_imports_post_rollback_rows_once(self):
        db = self.make_db()
        db.upsert_rating(
            "enso",
            {"album_id": "history-1", "score": "80"},
        )
        db.record_score_change("enso", "history-1", "80", "90")

        rollback_timestamp = time.time() + 1
        db.connection.execute(
            "DELETE FROM meta WHERE key='change_history_legacy_watermark'"
        )
        db.connection.execute(
            """
            INSERT INTO rating_history(
                username, album_id, event_type,
                old_score, new_score, changed_at
            ) VALUES('enso', 'history-1', 'score', '90', '95', ?)
            """,
            (rollback_timestamp,),
        )
        db.connection.commit()
        db.close()

        reopened = self.make_db()
        try:
            events = reopened.get_change_history("enso", limit=20)
            self.assertEqual(len(events), 2)
            self.assertEqual(
                sum(event["source"] == "legacy" for event in events),
                1,
            )
            watermark = reopened.connection.execute(
                """
                SELECT value FROM meta
                WHERE key='change_history_legacy_watermark'
                """
            ).fetchone()[0]
            self.assertEqual(int(watermark), 2)
        finally:
            reopened.close()

        reopened_again = self.make_db()
        try:
            self.assertEqual(
                len(reopened_again.get_change_history("enso", limit=20)),
                2,
            )
        finally:
            reopened_again.close()

    def test_diagnostics_reports_foreign_key_violations(self):
        db = self.make_db()
        try:
            db.connection.execute("PRAGMA foreign_keys=OFF")
            db.connection.execute(
                """
                INSERT INTO change_history(
                    username, entity_type, event_type, source, detected_at
                ) VALUES('outside-config', 'profile', 'test', 'test', 1)
                """
            )
            db.connection.commit()
            db.connection.execute("PRAGMA foreign_keys=ON")

            stats = db.diagnostics()
            self.assertFalse(stats["healthy"])
            self.assertEqual(stats["quick_check"], "ok")
            self.assertEqual(stats["foreign_key_violations"], 1)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
