"""Focused regressions for AOTY transport, completeness and score ownership.

No test performs a real network request and this file intentionally stays
separate from the historical ``test_core.py`` suite.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parents[1]
TEST_RUNTIME = tempfile.mkdtemp(prefix="kotone-runtime-regressions-")
os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("DATA_DIR", TEST_RUNTIME)
sys.path.insert(0, str(ROOT))

import aoty  # noqa: E402
import http_client  # noqa: E402
import services  # noqa: E402
from database import Database  # noqa: E402
from http_client import (  # noqa: E402
    ExternalChallenge,
    ExternalRequestsPaused,
    PageResult,
    ResilientHTTPClient,
)


class ServiceScoreOwnershipTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="kotone-service-test-"))
        self.db = Database(
            str(self.tmp / "kotone.sqlite3"),
            monitored_users=("enso",),
            legacy_json_path=str(self.tmp / "missing.json"),
            migrated_backup_path=str(self.tmp / "migrated.bak"),
            backup_path=str(self.tmp / "backup.sqlite3"),
        )
        self.original_db = services.DB
        services.DB = self.db
        self.service = services.DataService()
        # Force the command path to perform its mocked live refresh.
        self.service._age = lambda _timestamp: float("inf")

    def tearDown(self):
        services.DB = self.original_db
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def _recent(self, item: dict, format_key: str):
        original = aoty.get_recent_ratings
        aoty.get_recent_ratings = (
            lambda *_args, **_kwargs: aoty.RatingsResult([item])
        )
        try:
            return await self.service.get_recent_ratings(
                "enso",
                count=10,
                format_key=format_key,
            )
        finally:
            aoty.get_recent_ratings = original

    async def test_artist_autocomplete_uses_exact_sqlite_match_without_live_call(self):
        self.db.upsert_rating(
            "enso",
            {
                "album_id": "783921",
                "score": "91",
                "artist": "ARTMS",
                "album": "Dall",
                "url": "https://www.albumoftheyear.org/album/783921-artms-dall.php",
                "release_format": "LP",
            },
        )
        original = aoty.search_aoty_artists
        aoty.search_aoty_artists = lambda *_args, **_kwargs: self.fail(
            "exact SQLite autocomplete must not call AOTY"
        )
        try:
            results = await self.service.search_artists("artms", limit=10)
        finally:
            aoty.search_aoty_artists = original

        self.assertEqual(results[0]["name"], "ARTMS")
        self.assertEqual(results[0]["value"], "ARTMS")
        self.assertEqual(results[0]["source"], "SQLite cache")

    async def test_artist_discography_falls_back_to_sqlite_on_live_failure(self):
        self.db.upsert_rating(
            "enso",
            {
                "album_id": "783921",
                "score": "91",
                "artist": "ARTMS",
                "artist_url": "https://www.albumoftheyear.org/artist/999-artms/",
                "album": "Dall",
                "url": "https://www.albumoftheyear.org/album/783921-artms-dall.php",
                "release_format": "LP",
            },
        )
        original = aoty.resolve_artist

        def fail_live(*_args, **_kwargs):
            raise RuntimeError("challenge")

        aoty.resolve_artist = fail_live
        try:
            artist, discography = await self.service.get_artist_discography(
                "ARTMS"
            )
        finally:
            aoty.resolve_artist = original

        self.assertEqual(artist["name"], "ARTMS")
        self.assertEqual(discography["source"], "SQLite cache")
        self.assertEqual(discography["releases"][0]["title"], "Dall")

    async def test_artist_discography_keeps_imported_image_when_lastfm_has_none(self):
        self.db.upsert_rating(
            "enso",
            {
                "album_id": "artist-image-1",
                "score": "83",
                "artist": "Portraits Of Tracy",
                "album": "Drive Home",
                "artist_url": "https://www.albumoftheyear.org/artist/142224-portraits-of-tracy/",
            },
        )
        self.db.save_release_details(
            "artist-image-1",
            {
                "artist": "Portraits Of Tracy",
                "album": "Drive Home",
                "artist_url": "https://www.albumoftheyear.org/artist/142224-portraits-of-tracy/",
            },
        )
        self.assertTrue(
            self.db.save_artist_source_data(
                "Portraits Of Tracy",
                "manual",
                {"image_url": "https://cdn.albumoftheyear.org/artists/portraits.jpg"},
                quality="operator_html",
                allow_unscoped_manual=True,
            )
        )

        _artist, discography = await self.service.get_artist_discography(
            "Portraits Of Tracy"
        )

        self.assertEqual(
            discography["image"],
            "https://cdn.albumoftheyear.org/artists/portraits.jpg",
        )

    async def test_interactive_enabled_refresh_preserves_score_and_pending(self):
        self.db.upsert_rating(
            "enso",
            {
                "album_id": "enabled-1",
                "score": "70",
                "album": "Album",
                "release_format": "LP",
            },
        )
        self.db.mark_sync_success("enso")
        self.db.set_notify_pending("enso", "enabled-1", True)

        live = await self._recent(
            {
                "album_id": "enabled-1",
                "score": "90",
                "album": "Album",
                "release_format": "LP",
            },
            "lp",
        )

        self.assertEqual(live[0]["score"], "90")
        stored = self.db.get_ratings_map(
            "enso",
            include_inactive=True,
        )["enabled-1"]
        self.assertEqual(stored["score"], "70")
        self.assertTrue(stored["notify_pending"])

    async def test_interactive_new_enabled_rating_becomes_pending(self):
        self.db.mark_sync_success("enso")

        await self._recent(
            {
                "album_id": "enabled-new",
                "score": "88",
                "album": "New Album",
                "release_format": "LP",
            },
            "lp",
        )

        stored = self.db.get_ratings_map(
            "enso",
            include_inactive=True,
        )["enabled-new"]
        self.assertEqual(stored["score"], "88")
        self.assertTrue(stored["notify_pending"])

    async def test_disabled_format_refresh_records_post_baseline_score_change(self):
        self.db.upsert_rating(
            "enso",
            {
                "album_id": "holiday-1",
                "score": "60",
                "album": "Holiday Album",
                "release_format": "Holiday",
            },
        )
        self.db.mark_sync_success("enso")

        await self._recent(
            {
                "album_id": "holiday-1",
                "score": "75",
                "album": "Holiday Album",
                "release_format": "Holiday",
            },
            "holiday",
        )

        stored = self.db.get_rating("enso", "holiday-1")
        self.assertEqual(stored["score"], "75")
        events = self.db.get_change_history("enso", album_id="holiday-1")
        self.assertTrue(
            any(event["event_type"] == "score_changed" for event in events)
        )

    async def test_partial_profile_preserves_favorites_but_explicit_empty_removes(self):
        self.db.save_profile(
            "enso",
            {
                "username": "enso",
                "url": "https://www.albumoftheyear.org/user/enso/",
                "favorite_kind": "albums",
                "favorites": [
                    {
                        "type": "album",
                        "name": "Favorite",
                        "artist": "Artist",
                        "album": "Favorite",
                        "url": "https://www.albumoftheyear.org/album/1-favorite.php",
                    }
                ],
            },
        )

        original = aoty.get_profile_summary
        try:
            aoty.get_profile_summary = lambda _username: {
                "username": "enso",
                "url": "https://www.albumoftheyear.org/user/enso/",
                "favorites_complete": False,
                "favorite_kind": None,
                "favorites": [],
            }
            await self.service.sync_profile("enso")
            self.assertEqual(len(self.db.get_profile("enso")["favorites"]), 1)

            aoty.get_profile_summary = lambda _username: {
                "username": "enso",
                "url": "https://www.albumoftheyear.org/user/enso/",
                "favorites_complete": True,
                "favorite_kind": None,
                "favorites": [],
            }
            await self.service.sync_profile("enso")
            self.assertEqual(self.db.get_profile("enso")["favorites"], [])
        finally:
            aoty.get_profile_summary = original

    async def test_cached_track_scores_are_database_first(self):
        self.db.upsert_rating(
            "enso",
            {
                "album_id": "cached-tracks",
                "score": "91",
                "album": "Cached Tracks",
                "has_track_ratings": True,
            },
        )
        self.db.save_rating_detail(
            "enso",
            "cached-tracks",
            {
                "has_track_ratings": True,
                "track_ratings": [
                    {"number": 1, "title": "One", "score": "87"}
                ],
                "detail_incomplete": False,
            },
        )
        self.service._age = lambda _timestamp: 0.0
        original = aoty.get_user_rating_for_album
        aoty.get_user_rating_for_album = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("fresh SQLite detail must not call AOTY")
        )
        try:
            result = await self.service.get_user_rating_for_album(
                "enso",
                "cached-tracks",
                None,
                None,
                require_detail=True,
            )
        finally:
            aoty.get_user_rating_for_album = original

        self.assertEqual(result["source"], "SQLite cache")
        self.assertEqual(result["track_ratings"][0]["score"], "87")

    async def test_compact_commands_receive_every_cached_rating_detail(self):
        self.db.upsert_rating(
            "enso",
            {
                "album_id": "full-command-cache",
                "score": "93",
                "artist": "Artist",
                "album": "Full Cache",
                "has_review": True,
                "has_track_ratings": True,
                "liked": True,
            },
        )
        self.db.save_rating_detail(
            "enso",
            "full-command-cache",
            {
                "review_text": "Cached review",
                "has_review": True,
                "has_track_ratings": True,
                "liked": True,
                "track_ratings": [
                    {"number": 1, "title": "Opening", "score": "96"},
                ],
                "detail_complete": True,
            },
        )
        self.db.mark_sync_success("enso")
        self.db.save_profile(
            "enso",
            {
                "username": "enso",
                "url": "https://www.albumoftheyear.org/user/enso/",
                "favorites": [],
            },
        )
        self.service._age = lambda _timestamp: 0.0

        original_recent = aoty.get_recent_ratings
        original_detail = aoty.get_user_rating_for_album
        aoty.get_recent_ratings = lambda *_args, **_kwargs: self.fail(
            "fresh configured-user commands must not query AOTY"
        )
        aoty.get_user_rating_for_album = lambda *_args, **_kwargs: self.fail(
            "compact detail must come from SQLite"
        )
        try:
            recent = await self.service.get_recent_ratings("enso", 1)
            profile = await self.service.get_profile("enso", recent_limit=1)
            compact = await self.service.get_user_rating_for_album(
                "enso",
                "full-command-cache",
                None,
                None,
                require_detail=False,
            )
        finally:
            aoty.get_recent_ratings = original_recent
            aoty.get_user_rating_for_album = original_detail

        for item in (recent[0], profile["recent_ratings"][0], compact):
            self.assertEqual(item["review_text"], "Cached review")
            self.assertTrue(item["liked"])
            self.assertEqual(item["track_ratings"][0]["score"], "96")

    def test_compact_artist_release_is_enriched_from_public_sqlite_cache(self):
        self.db.upsert_rating(
            "enso",
            {
                "album_id": "cached-release",
                "score": "88",
                "artist": "Artist",
                "album": "Album",
                "url": "https://www.albumoftheyear.org/album/1-album.php",
            },
        )
        self.db.save_release_details(
            "cached-release",
            {
                "artist": "Artist",
                "artist_url": "https://www.albumoftheyear.org/artist/1-artist/",
                "album": "Album",
                "year": "2025",
                "labels": ["Cached Label"],
                "genres": ["Art Pop"],
                "album_format": "LP",
            },
        )

        merged = self.service.release_with_cached_details(
            {
                "album_id": "cached-release",
                "title": "Album",
                "url": "https://www.albumoftheyear.org/album/1-album.php",
            }
        )

        self.assertEqual(merged["year"], "2025")
        self.assertEqual(merged["labels"], ["Cached Label"])
        self.assertEqual(merged["genres"], ["Art Pop"])
        self.assertEqual(merged["album_format"], "LP")
        self.assertEqual(
            merged["artist_url"],
            "https://www.albumoftheyear.org/artist/1-artist/",
        )

        merged_with_empty_card_url = self.service.release_with_cached_details(
            {
                "album_id": "cached-release",
                "title": "Album",
                "artist_url": "",
            }
        )
        self.assertEqual(
            merged_with_empty_card_url["artist_url"],
            "https://www.albumoftheyear.org/artist/1-artist/",
        )

    async def test_stale_cached_track_scores_survive_challenge(self):
        self.db.upsert_rating(
            "enso",
            {
                "album_id": "stale-tracks",
                "score": "90",
                "album": "Stale Tracks",
                "has_track_ratings": True,
            },
        )
        self.db.save_rating_detail(
            "enso",
            "stale-tracks",
            {
                "has_track_ratings": True,
                "track_ratings": [
                    {"number": 1, "title": "One", "score": "86"}
                ],
                "detail_incomplete": False,
            },
        )
        original = aoty.get_user_rating_for_album

        def challenge(*_args, **_kwargs):
            raise aoty.AOTYChallengeCooldown("challenge", retry_after=3600)

        aoty.get_user_rating_for_album = challenge
        try:
            result = await self.service.get_user_rating_for_album(
                "enso",
                "stale-tracks",
                None,
                None,
                require_detail=True,
            )
        finally:
            aoty.get_user_rating_for_album = original

        self.assertEqual(result["source"], "SQLite cache")
        self.assertEqual(result["track_ratings"][0]["score"], "86")

    async def test_musicbrainz_fallback_is_used_only_after_aoty_failure(self):
        item = {
            "album_id": "mb-fallback",
            "score": "80",
            "artist": "Artist",
            "album": "Fallback Album",
            "url": "https://www.albumoftheyear.org/album/1-fallback.php",
            "release_format": "EP",
        }
        self.db.upsert_rating("enso", item)
        original_aoty = aoty.get_album_details
        original_mb = services.musicbrainz.MUSICBRAINZ.lookup_release
        calls = []

        def challenge(*_args, **_kwargs):
            raise aoty.AOTYChallengeCooldown("challenge", retry_after=3600)

        def fallback(*args, **kwargs):
            calls.append((args, kwargs))
            return {
                "source": "musicbrainz",
                "artist": "Artist",
                "album": "Fallback Album",
                "release_date": "2025-01-02",
                "year": "2025",
                "album_format": "EP",
                "genres": ["Art Pop"],
                "tracklist": [{"number": 1, "title": "Track", "duration": "3:00"}],
                "_section_complete": {
                    "score": False,
                    "release_date": True,
                    "format": True,
                    "labels": False,
                    "genres": True,
                    "vibes": False,
                    "ranking": False,
                    "tracklist": True,
                },
            }

        aoty.get_album_details = challenge
        services.musicbrainz.MUSICBRAINZ.lookup_release = fallback
        try:
            result = await self.service.enrich_user(
                "enso",
                detail_limit=0,
                release_limit=1,
            )
        finally:
            aoty.get_album_details = original_aoty
            services.musicbrainz.MUSICBRAINZ.lookup_release = original_mb

        self.assertEqual(result["musicbrainz"], 1)
        self.assertEqual(len(calls), 1)
        self.assertIsNotNone(self.db.get_release_details("mb-fallback"))
        cached = self.service.cached_release_details("mb-fallback")
        self.assertEqual(cached["metadata_source"], "musicbrainz")
        self.assertEqual(cached["metadata_sources"]["genres"], "musicbrainz")
        self.assertEqual(cached["genres"], ["Art Pop"])
        self.assertEqual(cached["tracklist"][0]["duration"], "3:00")

    async def test_musicbrainz_outage_blocks_followup_requests_globally(self):
        item = {"artist": "Artist", "album": "Album"}
        original_mb = services.musicbrainz.MUSICBRAINZ.lookup_release
        calls = []

        def unavailable(*_args, **_kwargs):
            calls.append(True)
            raise services.musicbrainz.MusicBrainzUnavailable(
                "HTTP 503",
                retry_after=15 * 60,
            )

        services.musicbrainz.MUSICBRAINZ.lookup_release = unavailable
        try:
            self.assertIsNone(
                await self.service._musicbrainz_release_fallback(
                    item,
                    priority=0,
                )
            )
            self.assertIsNone(
                await self.service._musicbrainz_release_fallback(
                    item,
                    priority=0,
                )
            )
        finally:
            services.musicbrainz.MUSICBRAINZ.lookup_release = original_mb

        self.assertEqual(len(calls), 1)
        self.assertGreater(self.service._musicbrainz_blocked_until, time.time())
        self.assertTrue(self.service.musicbrainz_status()["blocked"])

    async def test_db_only_personal_detail_queue_does_not_starve_musicbrainz(self):
        item = {
            "album_id": "kocorono",
            "artist": "Bloodthirsty Butchers",
            "album": "Kocorono",
            "url": "https://www.albumoftheyear.org/album/1-kocorono.php",
            "release_format": "LP",
            "score": "85",
            "has_track_ratings": True,
            "sort_timestamp": 1,
        }
        self.db.upsert_rating("enso", item)
        self.assertTrue(
            self.db.save_release_details(
                "kocorono",
                {"source": "aoty", "album_format": "LP"},
            )
        )
        self.db.upsert_rating(
            "enso",
            {
                "album_id": "newer-release",
                "artist": "Newer Artist",
                "album": "Newer Album",
                "url": "https://www.albumoftheyear.org/album/2-newer.php",
                "release_format": "LP",
                "score": "90",
                "sort_timestamp": 999,
            },
        )
        self.assertEqual(
            self.db.detail_enrichment_candidates("enso", 1)[0]["album_id"],
            "kocorono",
        )
        # Rendering details queues Kocorono but performs no MusicBrainz request.
        await self.service.get_release_details(item, allow_network=False)
        self.assertIn("kocorono", self.service._release_priority_queue)
        original_mb = services.musicbrainz.MUSICBRAINZ.lookup_release
        calls = []

        def fallback(artist, album, **_kwargs):
            calls.append((artist, album))
            return {
                "source": "musicbrainz",
                "release_date": "1996-10-23",
                "year": "1996",
                "album_format": "LP",
                "tracklist": [
                    {"number": 1, "title": "Track", "duration": "3:00"}
                ],
                "_section_complete": {
                    "score": False,
                    "release_date": True,
                    "format": True,
                    "labels": False,
                    "genres": False,
                    "vibes": False,
                    "ranking": False,
                    "tracklist": True,
                },
            }

        services.musicbrainz.MUSICBRAINZ.lookup_release = fallback
        try:
            result = await self.service.enrich_user(
                "enso",
                detail_limit=2,
                release_limit=1,
                musicbrainz_only=True,
            )
        finally:
            services.musicbrainz.MUSICBRAINZ.lookup_release = original_mb

        self.assertEqual(result["musicbrainz"], 1)
        self.assertEqual(calls, [("Bloodthirsty Butchers", "Kocorono")])
        self.assertEqual(
            self.service.cached_release_details("kocorono")["release_date"],
            "1996-10-23",
        )
        self.assertEqual(
            self.db.get_release_details("kocorono")["release_date"],
            "1996-10-23",
        )

    def test_persisted_musicbrainz_fills_only_missing_aoty_fields(self):
        self.db.upsert_rating(
            "enso",
            {
                "album_id": "mixed-source",
                "artist": "Artist",
                "album": "Album",
                "url": "https://www.albumoftheyear.org/album/3-album.php",
                "release_format": "LP",
                "score": "80",
            },
        )
        self.assertTrue(
            self.db.save_release_details(
                "mixed-source",
                {"source": "aoty", "album_format": "LP"},
            )
        )
        self.assertTrue(
            self.db.save_musicbrainz_fallback(
                "mixed-source",
                {
                    "source": "musicbrainz",
                    "album_format": "EP",
                    "release_date": "2025-01-02",
                    "year": "2025",
                    "genres": ["Art Pop"],
                    "_section_complete": {
                        "score": False,
                        "release_date": True,
                        "format": True,
                        "labels": False,
                        "genres": True,
                        "vibes": False,
                        "ranking": False,
                        "tracklist": False,
                    },
                },
            )
        )

        merged = self.service.cached_release_details("mixed-source")
        self.assertEqual(merged["album_format"], "LP")
        self.assertEqual(merged["metadata_sources"]["format"], "aoty")
        self.assertEqual(merged["release_date"], "2025-01-02")
        self.assertEqual(
            merged["metadata_sources"]["release_date"],
            "musicbrainz",
        )
        self.assertEqual(merged["genres"], ["Art Pop"])
        self.assertEqual(
            self.db.get_release_details("mixed-source")["genres"],
            ["Art Pop"],
        )
        self.assertTrue(
            self.db.save_release_details(
                "mixed-source",
                {
                    "source": "aoty",
                    "release_date": "January 3, 2025",
                    "year": "2025",
                    "genres": ["Post-Punk"],
                    "secondary_genres": [],
                    "_section_complete": {
                        "score": False,
                        "release_date": True,
                        "format": False,
                        "labels": False,
                        "genres": True,
                        "vibes": False,
                        "ranking": False,
                        "tracklist": False,
                    },
                },
            )
        )
        authoritative = self.db.get_release_details("mixed-source")
        self.assertEqual(authoritative["release_date"], "January 3, 2025")
        self.assertEqual(authoritative["genres"], ["Post-Punk"])
        self.assertEqual(authoritative["metadata_sources"]["release_date"], "aoty")
        self.assertEqual(authoritative["metadata_sources"]["genres"], "aoty")

    async def test_aoty_details_win_without_musicbrainz_fallback(self):
        item = {
            "album_id": "aoty-priority",
            "score": "80",
            "artist": "Artist",
            "album": "Priority Album",
            "url": "https://www.albumoftheyear.org/album/2-priority.php",
            "release_format": "LP",
        }
        self.db.upsert_rating("enso", item)
        original_aoty = aoty.get_album_details
        original_mb = services.musicbrainz.MUSICBRAINZ.lookup_release
        aoty.get_album_details = lambda *_args, **_kwargs: {
            "artist": "Artist",
            "album": "Priority Album",
            "ratings_count": "123",
            "release_date": "January 1, 2025",
            "album_format": "LP",
            "_section_complete": {
                "score": True,
                "release_date": True,
                "format": True,
                "labels": True,
                "genres": True,
                "vibes": True,
                "ranking": True,
                "tracklist": True,
            },
        }
        services.musicbrainz.MUSICBRAINZ.lookup_release = lambda *_args, **_kwargs: self.fail(
            "MusicBrainz must not run when AOTY returned details"
        )
        try:
            result = await self.service.enrich_user(
                "enso",
                detail_limit=0,
                release_limit=1,
            )
        finally:
            aoty.get_album_details = original_aoty
            services.musicbrainz.MUSICBRAINZ.lookup_release = original_mb

        self.assertEqual(result["musicbrainz"], 0)
        self.assertEqual(
            self.db.get_release_details("aoty-priority")["ratings_count"],
            "123",
        )
        self.assertEqual(
            self.db.get_release_details("aoty-priority")["metadata_source"],
            "aoty",
        )

    async def test_enrichment_stops_after_one_global_challenge(self):
        self.db.upsert_rating(
            "enso",
            {
                "album_id": "challenge-1",
                "score": "80",
                "album": "Challenge",
                "url": "https://example.test/album/1",
                "has_track_ratings": True,
            },
        )
        calls = []
        original = services._thread_call

        async def challenge(*args, **_kwargs):
            calls.append(args)
            raise aoty.AOTYChallengeCooldown("challenge", retry_after=3600)

        services._thread_call = challenge
        try:
            result = await self.service.enrich_user(
                "enso",
                detail_limit=2,
                release_limit=2,
            )
        finally:
            services._thread_call = original

        self.assertEqual(result["errors"], 1)
        self.assertEqual(len(calls), 1)


class HTTPRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="kotone-http-test-"))
        self.challenge_file = self.tmp / "challenge.json"
        self.db_only_file = self.tmp / "db-only.json"
        self.original_challenge_file = http_client.AOTY_CHALLENGE_STATE_FILE
        self.original_db_only_file = http_client.AOTY_DB_ONLY_STATE_FILE
        http_client.AOTY_CHALLENGE_STATE_FILE = str(self.challenge_file)
        http_client.AOTY_DB_ONLY_STATE_FILE = str(self.db_only_file)

    def tearDown(self):
        http_client.AOTY_CHALLENGE_STATE_FILE = self.original_challenge_file
        http_client.AOTY_DB_ONLY_STATE_FILE = self.original_db_only_file
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_db_only_pause_blocks_requests_and_survives_restart(self):
        client = ResilientHTTPClient()
        calls = []

        def forbidden_get(*_args, **_kwargs):
            calls.append(True)
            raise AssertionError("/dbonly must not contact AOTY")

        client.session.get = forbidden_get
        self.assertTrue(client.set_db_only(True, actor="enso"))
        self.assertTrue(self.db_only_file.exists())
        self.assertTrue(client.status()["db_only"])

        with self.assertRaises(ExternalRequestsPaused):
            client.get(
                "https://www.albumoftheyear.org/user/enso/",
                use_cache=False,
                allow_stale=False,
            )
        self.assertEqual(calls, [])

        restarted = ResilientHTTPClient()
        self.assertTrue(restarted.db_only_enabled())
        self.assertFalse(restarted.set_db_only(False, actor="enso"))
        self.assertFalse(restarted.status()["db_only"])

    def test_user_detail_does_not_fallback_during_global_challenge(self):
        original_fetch = aoty._fetch_user_release_page
        original_route = aoty._get_ratings_from_route

        def challenge(*_args, **_kwargs):
            raise aoty.AOTYChallengeCooldown("challenge", retry_after=3600)

        def forbidden_route(*_args, **_kwargs):
            raise AssertionError("challenge must not start a fallback route")

        aoty._fetch_user_release_page = challenge
        aoty._get_ratings_from_route = forbidden_route
        try:
            with self.assertRaises(aoty.AOTYChallengeCooldown):
                aoty.get_user_rating_for_album(
                    "enso",
                    "1",
                    "https://www.albumoftheyear.org/album/1-test.php",
                )
        finally:
            aoty._fetch_user_release_page = original_fetch
            aoty._get_ratings_from_route = original_route

    def test_fetch_page_exposes_challenge_as_safe_incomplete_page(self):
        original = aoty.HTTP.get

        def challenge(_url):
            raise ExternalChallenge("challenge cooldown", retry_after=321)

        aoty.HTTP.get = challenge
        try:
            with self.assertRaises(aoty.AOTYChallengeCooldown) as raised:
                aoty.fetch_page("https://www.albumoftheyear.org/user/enso/")
        finally:
            aoty.HTTP.get = original

        self.assertIsInstance(raised.exception, aoty.AOTYPageIncomplete)
        self.assertEqual(raised.exception.retry_after, 321)

    def test_challenge_opens_global_cooldown_and_resumes_after_expiry(self):
        class Response:
            status_code = 200
            url = "https://www.albumoftheyear.org/user/enso/"
            headers = {}

            def __init__(self, text):
                self.text = text

            def raise_for_status(self):
                return None

        client = ResilientHTTPClient()
        client._gate.min_interval = 0
        client._gate.maintenance_interval = 0
        responses = [
            Response("<html><div id='challenge-platform'></div></html>"),
            Response("<html><body>Album of the Year</body></html>"),
        ]
        calls = []

        def fake_get(url, timeout):
            calls.append((url, timeout))
            return responses.pop(0)

        client.session.get = fake_get

        with self.assertRaises(ExternalChallenge):
            client.get(
                "https://www.albumoftheyear.org/user/enso/",
                use_cache=False,
                allow_stale=False,
            )

        status = client.status()
        self.assertTrue(status["challenge_open"])
        self.assertGreater(status["challenge_seconds"], 0)
        self.assertEqual(status["consecutive_failures"], 0)

        # A different worker/URL shares the same client and must fail fast
        # without sending a second request during the cooldown.
        with self.assertRaises(ExternalChallenge):
            client.get(
                "https://www.albumoftheyear.org/album/1-test.php",
                use_cache=False,
                allow_stale=False,
            )
        self.assertEqual(len(calls), 1)

        # Simulate monotonic expiry without sleeping for the production delay.
        client._challenge_open_until = 0.0
        client._challenge_until_epoch = 0.0
        result = client.get(
            "https://www.albumoftheyear.org/album/1-test.php",
            use_cache=False,
            allow_stale=False,
        )
        self.assertIn("Album of the Year", result.text)
        self.assertEqual(len(calls), 2)
        self.assertFalse(client.status()["challenge_open"])

    def test_challenge_deadline_survives_a_new_client(self):
        first = ResilientHTTPClient()
        first._record_challenge("interstitial/challenge page")
        self.assertTrue(self.challenge_file.exists())

        restarted = ResilientHTTPClient()
        status = restarted.status()
        self.assertTrue(status["challenge_open"])
        self.assertGreater(status["challenge_seconds"], 0)
        self.assertIsNotNone(status["challenge_until_epoch"])

    def test_invalid_url_is_rejected_without_retry_or_circuit_failure(self):
        client = ResilientHTTPClient()
        calls = []

        def fake_get(url, timeout):
            calls.append((url, timeout))
            raise AssertionError("Nie wolno wykonywać requestu dla błędnego URL")

        client.session.get = fake_get

        for invalid_url in (None, "", "None", "/relative/path"):
            with self.subTest(url=invalid_url):
                with self.assertRaises(ValueError):
                    client.get(
                        invalid_url,
                        use_cache=False,
                        allow_stale=False,
                    )

        self.assertEqual(calls, [])
        self.assertEqual(client.status()["consecutive_failures"], 0)

    def test_stable_404_is_not_retried_or_counted_as_circuit_failure(self):
        class NotFoundResponse:
            status_code = 404
            text = "not found"
            url = "https://example.test/missing"
            headers = {}

            def raise_for_status(self):
                raise requests.HTTPError("404", response=self)

        client = ResilientHTTPClient()
        client._gate.min_interval = 0
        client._gate.maintenance_interval = 0
        calls = []

        def fake_get(url, timeout):
            calls.append((url, timeout))
            return NotFoundResponse()

        client.session.get = fake_get

        with self.assertRaises(requests.HTTPError):
            client.get(
                "https://example.test/missing",
                use_cache=False,
                allow_stale=False,
            )

        self.assertEqual(len(calls), 1)
        self.assertEqual(client.status()["consecutive_failures"], 0)


class ReleaseEnrichmentCandidateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="kotone-release-candidates-"))
        self.db = Database(
            str(self.tmp / "kotone.sqlite3"),
            monitored_users=("enso",),
            legacy_json_path=str(self.tmp / "missing.json"),
            migrated_backup_path=str(self.tmp / "migrated.bak"),
            backup_path=str(self.tmp / "backup.sqlite3"),
        )

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_release_enrichment_skips_ratings_without_album_url(self):
        self.db.upsert_rating(
            "enso",
            {
                "album_id": "missing-url",
                "album": "Missing URL",
                "url": None,
                "score": "70",
            },
        )
        self.db.upsert_rating(
            "enso",
            {
                "album_id": "valid-url",
                "album": "Valid URL",
                "url": (
                    "https://www.albumoftheyear.org/album/1-valid.php"
                ),
                "score": "80",
            },
        )

        candidates = self.db.release_enrichment_candidates("enso", 10)

        self.assertEqual(
            [item["album_id"] for item in candidates],
            ["valid-url"],
        )

    def test_legacy_partial_release_is_immediately_due_for_fallback(self):
        self.db.upsert_rating(
            "enso",
            {
                "album_id": "legacy-partial",
                "artist": "Bloodthirsty Butchers",
                "album": "Kocorono",
                "url": "https://www.albumoftheyear.org/album/1-kocorono.php",
                "score": "85",
            },
        )
        self.assertTrue(
            self.db.save_release_details(
                "legacy-partial",
                {"cover": "https://example.test/cover.jpg", "album_format": "LP"},
            )
        )

        candidates = self.db.release_enrichment_candidates(
            "enso",
            10,
            fallback_stale_before=time.time() - 24 * 60 * 60,
            aoty_stale_before=time.time() - 12 * 60 * 60,
        )
        self.assertEqual(
            [item["album_id"] for item in candidates],
            ["legacy-partial"],
        )
        self.assertEqual(
            self.db.release_enrichment_candidates(
                "enso",
                10,
                fallback_stale_before=time.time() - 24 * 60 * 60,
                aoty_stale_before=time.time() - 12 * 60 * 60,
                exclude_album_ids=("legacy-partial",),
            ),
            [],
        )


class ParserCompletenessTests(unittest.TestCase):
    def test_fetch_page_preserves_stale_transport_provenance(self):
        original = aoty.HTTP.get
        aoty.HTTP.get = lambda *_args, **_kwargs: PageResult(
            text="<html><body>cached</body></html>",
            url="https://example.test/page",
            status_code=200,
            stale=True,
            from_cache=True,
        )
        try:
            page = aoty.fetch_page("https://example.test/page")
        finally:
            aoty.HTTP.get = original

        self.assertIsInstance(page, aoty.AOTYPage)
        self.assertTrue(page.stale)
        self.assertTrue(page.from_cache)

    def test_archive_rejects_stale_page(self):
        html = (
            "<html><head><title>enso Ratings - Album of the Year</title>"
            "</head><body>"
            + ("valid shell " * 40)
            + "</body></html>"
        )
        stale_page = aoty.AOTYPage(
            html,
            url="https://www.albumoftheyear.org/user/enso/ratings/lp/",
            status_code=200,
            stale=True,
            from_cache=True,
        )
        original = aoty.fetch_page
        aoty.fetch_page = lambda *_args, **_kwargs: stale_page
        try:
            with self.assertRaisesRegex(
                aoty.AOTYArchiveIncomplete,
                "stale cache",
            ):
                aoty._get_ratings_from_route(
                    "enso",
                    slug="lp",
                    limit=None,
                    max_pages=5,
                )
        finally:
            aoty.fetch_page = original

    def test_archive_rejects_duplicate_page_as_incomplete(self):
        original_fetch = aoty.fetch_page
        original_parse = aoty._parse_ratings_soup
        aoty.fetch_page = lambda *_args, **_kwargs: "<html><body>fixture</body></html>"
        aoty._parse_ratings_soup = lambda *_args, **_kwargs: [
            {"album_id": "same", "score": "80", "release_format": "LP"}
        ]
        try:
            with self.assertRaises(aoty.AOTYArchiveIncomplete):
                aoty._get_ratings_from_route(
                    "enso",
                    slug="lp",
                    limit=None,
                    max_pages=5,
                )
        finally:
            aoty.fetch_page = original_fetch
            aoty._parse_ratings_soup = original_parse

    def test_archive_rejects_redirect_to_different_slug_or_page(self):
        def ratings_page(url: str):
            return aoty.AOTYPage(
                (
                    "<html><head><title>enso Ratings - Album of the Year</title>"
                    "</head><body>"
                    + ("ratings route shell " * 30)
                    + "</body></html>"
                ),
                url=url,
                status_code=200,
            )

        pages = [
            ratings_page(
                "https://www.albumoftheyear.org/user/enso/ratings/lp/"
            ),
            # AOTY/proxy redirected page 2 to the generic ratings route. The
            # old prefix-only identity check accepted this response.
            ratings_page(
                "https://www.albumoftheyear.org/user/enso/ratings/"
            ),
        ]
        original_fetch = aoty.fetch_page
        original_parse = aoty._parse_ratings_soup
        aoty.fetch_page = lambda *_args, **_kwargs: pages.pop(0)
        aoty._parse_ratings_soup = lambda *_args, **_kwargs: [
            {"album_id": "one", "score": "80", "release_format": "LP"}
        ]
        try:
            with self.assertRaisesRegex(
                aoty.AOTYArchiveIncomplete,
                "unexpected final route",
            ):
                aoty._get_ratings_from_route(
                    "enso",
                    slug="lp",
                    limit=None,
                    forced_format="LP",
                    max_pages=5,
                )
        finally:
            aoty.fetch_page = original_fetch
            aoty._parse_ratings_soup = original_parse

    def test_archive_rejects_empty_shell_from_wrong_route(self):
        page = aoty.AOTYPage(
            (
                "<html><head><title>enso - Profile - Album of the Year</title>"
                "</head><body>"
                + ("profile shell " * 40)
                + "</body></html>"
            ),
            url="https://www.albumoftheyear.org/user/enso/",
            status_code=200,
        )
        original = aoty.fetch_page
        aoty.fetch_page = lambda *_args, **_kwargs: page
        try:
            with self.assertRaises(aoty.AOTYArchiveIncomplete):
                aoty._get_ratings_from_route(
                    "enso",
                    slug="lp",
                    limit=None,
                    max_pages=5,
                )
        finally:
            aoty.fetch_page = original

    def test_archive_accepts_identified_authoritative_empty_page(self):
        page = aoty.AOTYPage(
            (
                "<html><head><title>enso Ratings - Album of the Year</title>"
                "</head><body>"
                + ("ratings shell with no album cards " * 20)
                + "</body></html>"
            ),
            url="https://www.albumoftheyear.org/user/enso/ratings/lp/",
            status_code=200,
        )
        original = aoty.fetch_page
        aoty.fetch_page = lambda *_args, **_kwargs: page
        try:
            ratings = aoty._get_ratings_from_route(
                "enso",
                slug="lp",
                limit=None,
                max_pages=5,
            )
        finally:
            aoty.fetch_page = original

        self.assertEqual(ratings, [])
        self.assertFalse(ratings.stale)

    def test_archive_rejects_unparsed_rating_container(self):
        page = aoty.AOTYPage(
            (
                "<html><head><title>enso Ratings - Album of the Year</title>"
                "</head><body><div class='albumBlock'>"
                "<a href='/album/123-layout-change.php'>Album</a>"
                "<span class='new-score-layout'>88</span></div>"
                + ("ratings shell " * 30)
                + "</body></html>"
            ),
            url="https://www.albumoftheyear.org/user/enso/ratings/lp/",
            status_code=200,
        )
        original = aoty.fetch_page
        aoty.fetch_page = lambda *_args, **_kwargs: page
        try:
            with self.assertRaises(aoty.AOTYArchiveIncomplete):
                aoty._get_ratings_from_route(
                    "enso",
                    slug="lp",
                    limit=None,
                    max_pages=5,
                )
        finally:
            aoty.fetch_page = original

    def test_user_detail_interstitial_is_explicitly_incomplete(self):
        original = aoty.HTTP.get

        def fake_get(url, **_kwargs):
            return PageResult(
                text=(
                    "<html><head><title>Just a moment...</title></head>"
                    "<body><div class='cf-chl-test'>Verify you are human</div>"
                    + ("challenge " * 40)
                    + "</body></html>"
                ),
                url=url,
                status_code=200,
            )

        aoty.HTTP.get = fake_get
        try:
            soup, _url, complete = aoty._fetch_user_release_page(
                "enso",
                "123",
                "https://www.albumoftheyear.org/album/123-album.php",
                album_title="Album",
            )
        finally:
            aoty.HTTP.get = original

        self.assertIsNotNone(soup)
        self.assertFalse(complete)

    def test_album_interstitial_is_rejected_before_sparse_cache_write(self):
        page = aoty.AOTYPage(
            (
                "<html><head><title>Just a moment...</title></head>"
                "<body><div class='cf-chl-test'>Verify you are human</div>"
                + ("challenge " * 40)
                + "</body></html>"
            ),
            url="https://www.albumoftheyear.org/album/123-album.php",
            status_code=200,
        )
        original = aoty.fetch_page
        aoty.fetch_page = lambda *_args, **_kwargs: page
        try:
            with self.assertRaises(aoty.AOTYPageIncomplete):
                aoty.get_album_details(page.url)
        finally:
            aoty.fetch_page = original

    def test_album_heading_only_truncation_is_rejected(self):
        page = aoty.AOTYPage(
            (
                "<html><head><title>Album - Album of the Year</title></head>"
                "<body><h1>Album</h1>"
                + ("truncated after heading " * 25)
                + "</body></html>"
            ),
            url="https://www.albumoftheyear.org/album/123-album.php",
            status_code=200,
        )
        original = aoty.fetch_page
        aoty.fetch_page = lambda *_args, **_kwargs: page
        try:
            with self.assertRaises(aoty.AOTYPageIncomplete):
                aoty.get_album_details(page.url)
        finally:
            aoty.fetch_page = original

    def test_partial_valid_release_preserves_complete_cached_sections(self):
        tmp = Path(tempfile.mkdtemp(prefix="kotone-release-merge-"))
        db = Database(
            str(tmp / "kotone.sqlite3"),
            monitored_users=("enso",),
            legacy_json_path=str(tmp / "missing.json"),
            migrated_backup_path=str(tmp / "migrated.bak"),
            backup_path=str(tmp / "backup.sqlite3"),
        )
        db.upsert_rating(
            "enso",
            {
                "album_id": "123",
                "score": "90",
                "artist": "Artist",
                "album": "Album",
                "release_format": "LP",
            },
        )
        baseline = {
            "artist": "Artist",
            "artist_url": "https://www.albumoftheyear.org/artist/1-artist/",
            "album": "Album",
            "url": "https://www.albumoftheyear.org/album/123-album.php",
            "user_score": "80",
            "ratings_count": "1,234",
            "release_date": "January 1, 2025",
            "year": "2025",
            "album_format": "LP",
            "label": "Label",
            "labels": ["Label"],
            "genres": ["Art Pop"],
            "secondary_genres": ["Electronic"],
            "vibes": ["Atmospheric"],
            "ranking_year": "2025",
            "year_ranking": "12",
            "year_ranking_text": "#12",
            "tracklist": [
                {
                    "number": 1,
                    "title": "Track One",
                    "duration": "3:00",
                    "url": "https://www.albumoftheyear.org/song/1-track-one/",
                }
            ],
        }

        page = aoty.AOTYPage(
            (
                "<html><head><title>Album - Album of the Year</title></head>"
                "<body><a href='/artist/1-artist/'>Artist</a><h1>Album</h1>"
                "<div>User Score</div><span>85</span>"
                + ("valid but partially rendered release shell " * 20)
                + "</body></html>"
            ),
            url=baseline["url"],
            status_code=200,
        )
        original = aoty.fetch_page
        try:
            self.assertTrue(db.save_release_details("123", baseline))
            aoty.fetch_page = lambda *_args, **_kwargs: page
            partial = aoty.get_album_details(page.url)
            self.assertFalse(partial["_section_complete"]["labels"])
            self.assertFalse(partial["_section_complete"]["genres"])
            self.assertFalse(partial["_section_complete"]["tracklist"])
            self.assertTrue(db.save_release_details("123", partial))

            cached = db.get_release_details("123")
            self.assertEqual(cached["user_score"], "85")
            self.assertEqual(cached["release_date"], "January 1, 2025")
            self.assertEqual(cached["album_format"], "LP")
            self.assertEqual(cached["labels"], ["Label"])
            self.assertEqual(cached["genres"], ["Art Pop"])
            self.assertEqual(cached["secondary_genres"], ["Electronic"])
            self.assertEqual(cached["vibes"], ["Atmospheric"])
            self.assertEqual(cached["year_ranking"], "12")
            self.assertEqual(
                [track["title"] for track in cached["tracklist"]],
                ["Track One"],
            )
        finally:
            aoty.fetch_page = original
            db.close()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_profile_distinguishes_explicit_empty_favorites_from_missing(self):
        def profile_page(body: str):
            return aoty.AOTYPage(
                (
                    "<html><head><title>enso - Profile - Album of the Year</title>"
                    "</head><body><h1>enso</h1>"
                    + body
                    + ("profile shell " * 30)
                    + "</body></html>"
                ),
                url="https://www.albumoftheyear.org/user/enso/",
                status_code=200,
            )

        pages = [
            profile_page(
                "<h2>Favorites</h2><p>No favorites yet.</p>"
                "<h2>Recently Rated</h2>"
            ),
            profile_page("<h2>Recently Rated</h2>"),
        ]
        original = aoty.fetch_page
        try:
            aoty.fetch_page = lambda *_args, **_kwargs: pages[0]
            explicit_empty = aoty.get_profile_summary("enso")
            aoty.fetch_page = lambda *_args, **_kwargs: pages[1]
            missing = aoty.get_profile_summary("enso")
        finally:
            aoty.fetch_page = original

        self.assertTrue(explicit_empty["favorites_complete"])
        self.assertEqual(explicit_empty["favorites"], [])
        self.assertFalse(missing["favorites_complete"])


if __name__ == "__main__":
    unittest.main()
