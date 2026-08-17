"""Regression tests for shutdown, runtime safety, workers and detail views.

The guard/lifecycle tests use only the standard library.  UI/worker/health
tests are skipped when this file is run in a minimal interpreter without the
project dependencies; CI installs requirements.txt and executes all of them.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch


ROOT = Path(__file__).resolve().parents[1]
TEST_RUNTIME = tempfile.mkdtemp(prefix="kotone-lifecycle-tests-")
os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("DATA_DIR", TEST_RUNTIME)
atexit.register(shutil.rmtree, TEST_RUNTIME, ignore_errors=True)

from lifecycle import (  # noqa: E402
    SHUTDOWN_BACKUP_RESERVE_SECONDS,
    SHUTDOWN_HARD_EXIT_SECONDS,
    SHUTDOWN_TOTAL_SECONDS,
    arm_hard_exit_watchdog,
    persist_before_client_close,
    stop_tasks_before_deadline,
)
from runtime_guard import validate_persistent_runtime  # noqa: E402


PROJECT_IMPORT_ERROR = None
try:
    import discord  # noqa: E402
    import background as background_module  # noqa: E402
    import bot as bot_module  # noqa: E402
    import health as health_module  # noqa: E402
    import views as views_module  # noqa: E402
except ModuleNotFoundError as exc:  # pragma: no cover - exercised in minimal local runtime
    PROJECT_IMPORT_ERROR = exc


class RuntimeGuardTests(unittest.TestCase):
    def test_local_development_does_not_require_volume(self):
        validate_persistent_runtime({})

    def test_railway_without_volume_fails_fast(self):
        with self.assertRaisesRegex(RuntimeError, "Volume"):
            validate_persistent_runtime({"RAILWAY_PROJECT_ID": "project"})

    def test_railway_data_dir_must_be_on_volume(self):
        with self.assertRaisesRegex(RuntimeError, "DATA_DIR"):
            validate_persistent_runtime(
                {
                    "RAILWAY_PROJECT_ID": "project",
                    "RAILWAY_VOLUME_MOUNT_PATH": str(ROOT / "volume"),
                    "DATA_DIR": str(ROOT / "ephemeral"),
                }
            )

    def test_railway_accepts_volume_subdirectory(self):
        volume = ROOT / "volume"
        validate_persistent_runtime(
            {
                "RAILWAY_PROJECT_ID": "project",
                "RAILWAY_VOLUME_MOUNT_PATH": str(volume),
                "DATA_DIR": str(volume / "kotone"),
            }
        )

    def test_explicit_non_railway_production_requires_data_dir(self):
        with self.assertRaisesRegex(RuntimeError, "DATA_DIR"):
            validate_persistent_runtime({"KOTONE_ENV": "production"})
        validate_persistent_runtime(
            {
                "KOTONE_ENV": "production",
                "DATA_DIR": str(ROOT / "persistent"),
            }
        )


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_budget_stays_below_railway_drain(self):
        railway = json.loads((ROOT / "railway.json").read_text(encoding="utf-8"))
        draining_seconds = float(railway["deploy"]["drainingSeconds"])

        self.assertLess(SHUTDOWN_TOTAL_SECONDS, draining_seconds)
        self.assertLess(SHUTDOWN_HARD_EXIT_SECONDS, draining_seconds)
        self.assertGreater(SHUTDOWN_BACKUP_RESERVE_SECONDS, 0)
        self.assertLess(
            SHUTDOWN_BACKUP_RESERVE_SECONDS,
            SHUTDOWN_TOTAL_SECONDS,
        )

    async def test_hard_exit_watchdog_is_daemonized(self):
        fake_timer = SimpleNamespace(daemon=False, name=None, start=Mock())
        with patch("lifecycle.threading.Timer", return_value=fake_timer) as factory:
            result = arm_hard_exit_watchdog(12.5)

        self.assertIs(result, fake_timer)
        self.assertTrue(fake_timer.daemon)
        self.assertEqual(fake_timer.name, "kotone-railway-hard-exit")
        fake_timer.start.assert_called_once_with()
        self.assertEqual(factory.call_args.args[0], 12.5)
        self.assertEqual(factory.call_args.kwargs["args"], (0,))

    async def test_fatal_watchdog_uses_nonzero_exit(self):
        fake_timer = SimpleNamespace(daemon=False, name=None, start=Mock())
        with patch("lifecycle.threading.Timer", return_value=fake_timer) as factory:
            arm_hard_exit_watchdog(12.5, exit_code=1)

        self.assertEqual(factory.call_args.kwargs["args"], (1,))

    async def test_persistence_finishes_before_hung_client_close_times_out(self):
        persisted = asyncio.Event()

        async def persistence():
            persisted.set()
            return "backup-complete"

        async def hung_close():
            await asyncio.Event().wait()

        result, closed = await persist_before_client_close(
            persistence(),
            hung_close(),
            deadline=asyncio.get_running_loop().time() + 0.03,
        )

        self.assertTrue(persisted.is_set())
        self.assertEqual(result, "backup-complete")
        self.assertFalse(closed)

    async def test_workers_share_deadline_and_finish_normally(self):
        first = asyncio.create_task(asyncio.sleep(0.01))
        second = asyncio.create_task(asyncio.sleep(0.01))
        loop = asyncio.get_running_loop()

        result = await stop_tasks_before_deadline(
            {"monitor": first, "background": second},
            deadline=loop.time() + 1.0,
            reserve_seconds=0.0,
        )

        self.assertFalse(result.forced)
        self.assertEqual(set(result.completed), {"monitor", "background"})

    async def test_overdue_workers_are_cancelled_before_database_cleanup(self):
        first = asyncio.create_task(asyncio.sleep(60))
        second = asyncio.create_task(asyncio.sleep(60))
        loop = asyncio.get_running_loop()

        result = await stop_tasks_before_deadline(
            {"monitor": first, "background": second},
            deadline=loop.time() + 0.03,
            reserve_seconds=0.0,
        )

        self.assertTrue(result.forced)
        self.assertEqual(set(result.cancelled), {"monitor", "background"})
        self.assertEqual(result.still_pending, ())
        await asyncio.sleep(0)
        self.assertTrue(first.cancelled())
        self.assertTrue(second.cancelled())


@unittest.skipIf(
    PROJECT_IMPORT_ERROR is not None,
    f"project dependencies unavailable: {PROJECT_IMPORT_ERROR}",
)
class BackgroundFairnessTests(unittest.IsolatedAsyncioTestCase):
    async def test_cursor_advances_after_user_that_really_did_archive_work(self):
        worker = background_module.BackgroundWorker(SimpleNamespace())
        calls = []

        async def archive(username, *, formats_per_cycle, priority):
            calls.append((username, formats_per_cycle))
            attempted = 0 if username == "a" else 1
            return {
                "formats_attempted": attempted,
                "errors": 0,
                "ratings": attempted,
            }

        with (
            patch.object(background_module, "USERS", ["a", "b", "c"]),
            patch.object(
                background_module,
                "PROFILE_RATING_ARCHIVE_FORMATS_PER_CYCLE",
                3,
            ),
            patch.object(
                background_module.DATA,
                "archive_profile_ratings",
                side_effect=archive,
            ),
        ):
            await worker._run_once()
            await worker._run_once()

        self.assertEqual(calls, [("a", 3), ("b", 3), ("c", 3)])

    async def test_enrichment_has_an_independent_fair_cursor(self):
        worker = background_module.BackgroundWorker(SimpleNamespace())
        enrich_calls = []

        async def no_archive(username, *, formats_per_cycle, priority):
            return {"formats_attempted": 0, "errors": 0, "ratings": 0}

        async def enrich(username):
            enrich_calls.append(username)
            work = username != "a"
            return {"errors": 0, "releases": int(work), "details": 0}

        with (
            patch.object(background_module, "USERS", ["a", "b", "c"]),
            patch.object(
                background_module.DATA,
                "archive_profile_ratings",
                side_effect=no_archive,
            ),
            patch.object(worker, "_enrich_one_user", side_effect=enrich),
        ):
            await worker._run_once()
            await worker._run_once()

        self.assertEqual(enrich_calls, ["a", "b", "c"])


@unittest.skipIf(
    PROJECT_IMPORT_ERROR is not None,
    f"project dependencies unavailable: {PROJECT_IMPORT_ERROR}",
)
class DetailViewTests(unittest.IsolatedAsyncioTestCase):
    async def test_album_compact_card_reloads_complete_track_rows(self):
        compact = {
            "score": "90",
            "review_text": "review",
            "has_review": True,
            "has_track_ratings": True,
            "detail_complete": True,
        }
        complete = {
            **compact,
            "detail_incomplete": False,
            "track_ratings": [
                {"number": 1, "title": "Track", "score": "88"}
            ],
        }
        view = views_module.AlbumRatingView(
            main_embed=discord.Embed(),
            release_item={"album_id": "1", "album": "Album"},
            usernames=["enso"],
            rating_infos={"enso": compact},
        )

        with patch.object(
            views_module,
            "_load_live_extra",
            new=AsyncMock(return_value=complete),
        ) as loader:
            result = await view._extra_for_selected()

        loader.assert_awaited_once()
        fallback_item = loader.await_args.args[1]
        self.assertTrue(fallback_item["has_review"])
        self.assertTrue(fallback_item["has_track_ratings"])
        self.assertEqual(result["track_ratings"][0]["score"], "88")

    async def test_profile_track_failure_is_not_reported_as_no_ratings(self):
        item = {
            "album_id": "1",
            "album": "Album",
            "has_track_ratings": True,
        }
        view = views_module.ProfilePagerView(
            username="enso",
            ratings=[item],
            build_page_embed=lambda page: discord.Embed(),
        )
        view._extra = AsyncMock(
            return_value={
                "detail_incomplete": True,
                "has_track_ratings": True,
                "track_ratings": [],
            }
        )
        interaction = SimpleNamespace(
            response=SimpleNamespace(
                defer=AsyncMock(),
                send_message=AsyncMock(),
            ),
            followup=SimpleNamespace(send=AsyncMock()),
            message=SimpleNamespace(edit=AsyncMock()),
        )

        await view._tracks(interaction)

        warning = interaction.followup.send.await_args.args[0]
        self.assertIn("nie zostały teraz pobrane", warning)
        interaction.message.edit.assert_not_awaited()

    async def test_profile_review_failure_is_not_reported_as_no_review(self):
        item = {
            "album_id": "1",
            "album": "Album",
            "has_review": True,
        }
        view = views_module.ProfilePagerView(
            username="enso",
            ratings=[item],
            build_page_embed=lambda page: discord.Embed(),
        )
        view._extra = AsyncMock(
            return_value={
                "detail_incomplete": True,
                "has_review": True,
                "review_text": None,
            }
        )
        interaction = SimpleNamespace(
            response=SimpleNamespace(
                defer=AsyncMock(),
                send_message=AsyncMock(),
            ),
            followup=SimpleNamespace(send=AsyncMock()),
            message=SimpleNamespace(edit=AsyncMock()),
        )

        await view._review(interaction)

        warning = interaction.followup.send.await_args.args[0]
        self.assertIn("treść nie została teraz pobrana", warning)
        interaction.message.edit.assert_not_awaited()


@unittest.skipIf(
    PROJECT_IMPORT_ERROR is not None,
    f"project dependencies unavailable: {PROJECT_IMPORT_ERROR}",
)
class HealthWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_running_workers_keep_readiness_healthy(self):
        class Client:
            @staticmethod
            def is_ready():
                return True

            @staticmethod
            def is_closed():
                return False

        async def wait_forever():
            await asyncio.Event().wait()

        monitor_task = asyncio.create_task(wait_forever())
        background_task = asyncio.create_task(wait_forever())
        server = health_module.HealthServer(
            Client(),
            SimpleNamespace(last_success_at=None),
            SimpleNamespace(last_success_at=None),
        )
        server.bind_worker_tasks(
            monitor_task=monitor_task,
            background_task=background_task,
        )

        try:
            with (
                patch.object(health_module.DB, "health", return_value=True),
                patch.object(health_module.HTTP, "status", return_value={}),
            ):
                response = await server._health(None)

            payload = json.loads(response.text)
            self.assertEqual(response.status, 200)
            self.assertTrue(payload["monitor_ok"])
            self.assertTrue(payload["background_ok"])
        finally:
            monitor_task.cancel()
            background_task.cancel()
            await asyncio.gather(
                monitor_task,
                background_task,
                return_exceptions=True,
            )

    async def test_dead_background_worker_makes_readiness_fail(self):
        class Client:
            @staticmethod
            def is_ready():
                return True

            @staticmethod
            def is_closed():
                return False

        async def wait_forever():
            await asyncio.Event().wait()

        async def fail():
            raise RuntimeError("worker died")

        monitor_task = asyncio.create_task(wait_forever())
        background_task = asyncio.create_task(fail())
        await asyncio.sleep(0)
        server = health_module.HealthServer(
            Client(),
            SimpleNamespace(last_success_at=None),
            SimpleNamespace(last_success_at=None),
        )
        server.bind_worker_tasks(
            monitor_task=monitor_task,
            background_task=background_task,
        )

        try:
            with (
                patch.object(health_module.DB, "health", return_value=True),
                patch.object(health_module.HTTP, "status", return_value={}),
            ):
                response = await server._health(None)

            payload = json.loads(response.text)
            self.assertEqual(response.status, 503)
            self.assertTrue(payload["monitor_ok"])
            self.assertFalse(payload["background_ok"])
            self.assertEqual(payload["workers"]["background"], "failed")
        finally:
            monitor_task.cancel()
            await asyncio.gather(monitor_task, return_exceptions=True)


@unittest.skipIf(
    PROJECT_IMPORT_ERROR is not None,
    f"project dependencies unavailable: {PROJECT_IMPORT_ERROR}",
)
class WorkerSupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def test_unexpected_worker_exit_requests_nonzero_shutdown(self):
        async def fail():
            raise RuntimeError("worker died")

        task = asyncio.create_task(fail(), name="test-worker")
        await asyncio.gather(task, return_exceptions=True)

        with (
            patch.object(bot_module, "shutdown_requested", False),
            patch.object(bot_module.client, "is_closed", return_value=False),
            patch.object(bot_module, "_schedule_shutdown") as schedule,
        ):
            bot_module._log_worker_exit(task)

        schedule.assert_called_once_with(exit_code=1)

    async def test_expected_worker_exit_during_shutdown_is_ignored(self):
        task = asyncio.create_task(asyncio.sleep(0), name="test-worker")
        await task

        with (
            patch.object(bot_module, "shutdown_requested", True),
            patch.object(bot_module, "_schedule_shutdown") as schedule,
        ):
            bot_module._log_worker_exit(task)

        schedule.assert_not_called()


if __name__ == "__main__":
    unittest.main()
