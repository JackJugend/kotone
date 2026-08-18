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
    import shared as shared_module  # noqa: E402
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
    async def test_db_only_skips_aoty_but_allows_musicbrainz_fallback(self):
        worker = background_module.BackgroundWorker(SimpleNamespace())
        calls = []

        async def enrich(username, *, musicbrainz_only=False):
            calls.append((username, musicbrainz_only))
            return {"releases": 1, "details": 0, "errors": 0}

        with (
            patch.object(background_module, "USERS", ["enso"]),
            patch.object(background_module.HTTP, "db_only_enabled", return_value=True),
            patch.object(worker, "_enrich_one_user", side_effect=enrich),
            patch.object(
                background_module.DATA,
                "archive_profile_ratings",
                side_effect=AssertionError("/dbonly must not call AOTY archive"),
            ),
        ):
            await worker._run_once()

        self.assertEqual(calls, [("enso", True)])

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
class SharedAssetTests(unittest.TestCase):
    def test_aoty_footer_asset_is_applied_by_shared_helper(self):
        embed = discord.Embed()
        shared_module.set_aoty_footer(embed, "SQLite cache")

        self.assertEqual(embed.footer.text, "SQLite cache")
        self.assertEqual(
            embed.footer.icon_url,
            shared_module.AOTY_ICON_ATTACHMENT,
        )


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

    async def test_profile_track_action_uses_combined_tracklist(self):
        item = {
            "album_id": "1",
            "artist": "Artist",
            "album": "Album",
            "url": "https://www.albumoftheyear.org/album/1-album/",
        }
        view = views_module.ProfilePagerView(
            username="enso",
            ratings=[item],
            build_page_embed=lambda page: discord.Embed(),
        )
        interaction = SimpleNamespace(
            response=SimpleNamespace(
                defer=AsyncMock(),
                send_message=AsyncMock(),
            ),
            followup=SimpleNamespace(send=AsyncMock()),
            message=SimpleNamespace(edit=AsyncMock()),
        )

        combined = discord.Embed(description="combined")
        with patch.object(
            views_module,
            "build_combined_tracklist_embed",
            new=AsyncMock(return_value=combined),
        ) as builder:
            await view._tracks(interaction)

        builder.assert_awaited_once_with(item)
        interaction.message.edit.assert_awaited_once_with(embed=combined, view=view)

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

    async def test_shared_action_order_and_disabled_review_are_consistent(self):
        item = {
            "album_id": "1",
            "artist": "Artist",
            "album": "Album",
            "url": "https://www.albumoftheyear.org/album/1-album/",
        }
        views = [
            views_module.SingleRatingView(
                username="enso",
                item=item,
                main_embed=discord.Embed(),
            ),
            views_module.MultiRatingView(
                username="enso",
                items=[item],
                main_embeds=[discord.Embed()],
            ),
            views_module.AlbumRatingView(
                main_embed=discord.Embed(),
                release_item=item,
                usernames=["enso"],
                rating_infos={"enso": {}},
            ),
            views_module.ProfilePagerView(
                username="enso",
                ratings=[item],
                build_page_embed=lambda page: discord.Embed(),
            ),
        ]

        for view in views:
            buttons = [
                child
                for child in view.children
                if isinstance(child, discord.ui.Button)
                and child.label in views_module.ACTION_BUTTON_ORDER
            ]
            self.assertEqual(
                [button.label for button in buttons],
                ["★", "🛈", "🏠︎", "☰", "✎"],
            )
            review = next(button for button in buttons if button.label == "✎")
            self.assertTrue(review.disabled)
            active = [
                button.label
                for button in buttons
                if button.style == discord.ButtonStyle.primary
            ]
            self.assertEqual(active, ["🏠︎"])

    async def test_profile_navigation_is_stable_only_on_home_tab(self):
        ratings = [
            {
                "album_id": str(index),
                "artist": "Artist",
                "album": f"Album {index}",
                "url": f"https://www.albumoftheyear.org/album/{index}-album/",
            }
            for index in range(1, 7)
        ]
        view = views_module.ProfilePagerView(
            username="enso",
            ratings=ratings,
            build_page_embed=lambda page: discord.Embed(),
        )

        def arrows():
            return [
                child
                for child in view.children
                if isinstance(child, discord.ui.Button)
                and child.label in {"←", "→"}
            ]

        first_page = arrows()
        self.assertEqual([button.label for button in first_page], ["←", "→"])
        self.assertTrue(first_page[0].disabled)
        self.assertFalse(first_page[1].disabled)

        view.page_index = 1
        view.selected_index = 5
        view._rebuild_components()
        last_page = arrows()
        self.assertFalse(last_page[0].disabled)
        self.assertTrue(last_page[1].disabled)

        view.current_tab = "☰"
        view._rebuild_components()
        self.assertEqual(arrows(), [])
        tracklist = next(
            child
            for child in view.children
            if isinstance(child, discord.ui.Button) and child.label == "☰"
        )
        self.assertEqual(tracklist.style, discord.ButtonStyle.primary)

    async def test_artist_action_is_public_and_updates_source_view(self):
        item = {
            "artist": "Artist",
            "album": "Album",
            "album_id": "1",
            "url": "https://www.albumoftheyear.org/album/1-album/",
        }
        source_view = views_module.SingleRatingView(
            username="enso",
            item=item,
            main_embed=discord.Embed(),
        )
        result_view = SimpleNamespace(bind_message=Mock())
        sent_message = SimpleNamespace()
        interaction = SimpleNamespace(
            response=SimpleNamespace(
                defer=AsyncMock(),
                send_message=AsyncMock(),
            ),
            followup=SimpleNamespace(
                send=AsyncMock(return_value=sent_message),
            ),
            message=SimpleNamespace(edit=AsyncMock()),
        )

        with patch(
            "commands.artist.build_artist_response",
            new=AsyncMock(return_value=(discord.Embed(), result_view)),
        ):
            await views_module._show_artist_command(
                interaction,
                item,
                source_view=source_view,
            )

        interaction.response.defer.assert_awaited_once_with()
        interaction.message.edit.assert_awaited_once_with(view=source_view)
        self.assertFalse(interaction.followup.send.await_args.kwargs["ephemeral"])
        result_view.bind_message.assert_called_once_with(sent_message)

    async def test_artist_result_is_reused_once_and_cleared_on_tab_change(self):
        item = {
            "artist": "Artist",
            "album": "Album",
            "album_id": "1",
            "url": "https://www.albumoftheyear.org/album/1-album/",
        }
        source_view = views_module.SingleRatingView(
            username="enso",
            item=item,
            main_embed=discord.Embed(),
        )
        artist_message = SimpleNamespace(
            edit=AsyncMock(),
            delete=AsyncMock(),
        )
        artist_message.edit.return_value = artist_message
        source_view.artist_message = artist_message
        result_view = SimpleNamespace(bind_message=Mock())
        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            message=SimpleNamespace(edit=AsyncMock()),
        )

        with patch(
            "commands.artist.build_artist_response",
            new=AsyncMock(return_value=(discord.Embed(), result_view)),
        ):
            await views_module._show_artist_command(
                interaction,
                item,
                source_view=source_view,
            )

        artist_message.edit.assert_awaited_once()
        interaction.followup.send.assert_not_awaited()
        self.assertIs(source_view.artist_message, artist_message)

        await views_module._clear_artist_result(source_view)
        artist_message.delete.assert_awaited_once_with()
        self.assertIsNone(source_view.artist_message)

    async def test_profile_select_contains_ratings_and_favorites(self):
        rating = {
            "album_id": "1",
            "artist": "Rated Artist",
            "album": "Rated Album",
            "url": "https://www.albumoftheyear.org/album/1-rated/",
        }
        view = views_module.ProfilePagerView(
            username="enso",
            ratings=[rating],
            favorites=[
                {
                    "type": "artist",
                    "name": "Favorite Artist",
                    "url": "https://www.albumoftheyear.org/artist/1-favorite/",
                },
                {
                    "type": "album",
                    "artist": "Album Artist",
                    "album": "Favorite Album",
                    "url": "https://www.albumoftheyear.org/album/2-favorite/",
                },
            ],
            build_page_embed=lambda page: discord.Embed(),
        )
        selector = next(
            child for child in view.children if isinstance(child, discord.ui.Select)
        )
        self.assertEqual(selector.placeholder, "Wybierz pozycję")
        self.assertEqual(
            {option.value for option in selector.options},
            {"rating:0", "favorite:0", "favorite:1"},
        )

        view.selected_source = "favorite"
        view.selected_index = 0
        view._rebuild_components()
        buttons = {
            child.label: child
            for child in view.children
            if isinstance(child, discord.ui.Button)
            and child.label in views_module.ACTION_BUTTON_ORDER
        }
        self.assertFalse(buttons["★"].disabled)
        self.assertTrue(buttons["🛈"].disabled)
        self.assertTrue(buttons["☰"].disabled)
        self.assertTrue(buttons["✎"].disabled)

    async def test_combined_tracklist_joins_public_and_config_user_scores(self):
        item = {
            "album_id": "7",
            "artist": "Artist",
            "album": "Album",
            "url": "https://www.albumoftheyear.org/album/7-album/",
        }
        details = {
            **item,
            "album_format": "LP",
            "tracklist": [
                {
                    "number": 1,
                    "title": "Opening Track",
                    "duration": "3:45",
                    "user_score": "82",
                }
            ],
        }
        cached = {
            "enso": {
                "detail_complete": True,
                "track_ratings": [
                    {"number": 1, "title": "Opening Track", "score": "90"}
                ],
            },
            "kulkien": {
                "detail_complete": True,
                "track_ratings": [
                    {"number": None, "title": "Opening Track", "score": "75"}
                ],
            },
        }
        with (
            patch.object(views_module, "USERS", ["enso", "kulkien"]),
            patch.object(
                views_module.DATA,
                "get_release_details",
                new=AsyncMock(return_value=details),
            ),
            patch.object(
                views_module.DATA,
                "cached_rating",
                side_effect=lambda username, album_id: cached[username],
            ),
            patch.object(
                views_module.DATA,
                "get_user_rating_for_album",
                new=AsyncMock(),
            ) as live_detail,
        ):
            embed = await views_module.build_combined_tracklist_embed(item)

        self.assertIn("**1.** Opening Track `3:45`", embed.description)
        self.assertIn("AOTY **82**", embed.description)
        self.assertIn("enso **90**", embed.description)
        self.assertIn("kulkien **75**", embed.description)
        live_detail.assert_not_awaited()

    async def test_details_tab_marks_aoty_and_musicbrainz_sources(self):
        item = {
            "album_id": "source-test",
            "artist": "Artist",
            "album": "Album",
            "url": "https://www.albumoftheyear.org/album/1-album/",
        }
        details = {
            **item,
            "user_score": "88",
            "release_date": "1996-12-01",
            "label": None,
            "metadata_sources": {
                "score": "aoty",
                "release_date": "musicbrainz",
            },
        }
        with (
            patch.object(
                views_module.DATA,
                "release_with_cached_details",
                return_value=item,
            ),
            patch.object(
                views_module.DATA,
                "get_release_details",
                new=AsyncMock(return_value=details),
            ),
        ):
            embed = await views_module.build_release_details_embed(item)

        self.assertIn(
            "<:aoty:1539095897084924004> **AOTY User Score:** 88",
            embed.description,
        )
        self.assertIn(
            "<:music_brainz:1539096206083629186> "
            "**Release date:** 1 grudnia 1996",
            embed.description,
        )
        self.assertIn("**Label:** —", embed.description)
        self.assertNotIn(
            "<:music_brainz:1539096206083629186> **Label:** —",
            embed.description,
        )

    async def test_tracklist_button_is_disabled_without_any_track_rows(self):
        item = {
            "album_id": "77",
            "artist": "Artist",
            "album": "Album",
            "url": "https://www.albumoftheyear.org/album/77-album/",
        }
        with (
            patch.object(
                views_module.DATA,
                "cached_release_details",
                return_value={"tracklist": []},
            ),
            patch.object(
                views_module.DATA,
                "cached_rating",
                return_value={"track_ratings": []},
            ),
        ):
            missing = views_module.SingleRatingView(
                username="enso",
                item=item,
                main_embed=discord.Embed(),
            )

        missing_tracklist = next(
            child
            for child in missing.children
            if isinstance(child, discord.ui.Button) and child.label == "☰"
        )
        self.assertTrue(missing_tracklist.disabled)

        with patch.object(
            views_module.DATA,
            "cached_release_details",
            return_value={"tracklist": [{"number": 1, "title": "Track"}]},
        ):
            available = views_module.SingleRatingView(
                username="enso",
                item=item,
                main_embed=discord.Embed(),
            )
        available_tracklist = next(
            child
            for child in available.children
            if isinstance(child, discord.ui.Button) and child.label == "☰"
        )
        self.assertFalse(available_tracklist.disabled)

    async def test_album_user_selector_exists_only_inside_review_tab(self):
        item = {
            "album_id": "1",
            "artist": "Artist",
            "album": "Album",
            "url": "https://www.albumoftheyear.org/album/1-album/",
        }
        infos = {
            "enso": {
                "score": "90",
                "has_review": True,
                "review_text": "Review enso",
                "track_ratings": [],
            },
            "kulkien": {
                "score": "80",
                "has_review": True,
                "review_text": "Review kulkien",
                "track_ratings": [],
            },
        }
        with patch.object(
            views_module.DATA,
            "cached_release_details",
            return_value={"tracklist": []},
        ):
            view = views_module.AlbumRatingView(
                main_embed=discord.Embed(),
                release_item=item,
                usernames=["enso", "kulkien"],
                rating_infos=infos,
            )

        self.assertNotIn(view.user_select, view.children)
        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            message=SimpleNamespace(edit=AsyncMock()),
        )
        await view._show_selected_review(interaction)
        self.assertIn(view.user_select, view.children)
        self.assertEqual(view.user_select.placeholder, "Wybierz użytkownika")

        view._set_user_selector_visible(False)
        self.assertNotIn(view.user_select, view.children)


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
