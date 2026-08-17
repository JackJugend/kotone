"""Focused regressions for AOTY transport, completeness and score ownership.

No test performs a real network request and this file intentionally stays
separate from the historical ``test_core.py`` suite.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parents[1]
TEST_RUNTIME = tempfile.mkdtemp(prefix="kotone-runtime-regressions-")
os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("DATA_DIR", TEST_RUNTIME)
sys.path.insert(0, str(ROOT))

import aoty  # noqa: E402
import services  # noqa: E402
from database import Database  # noqa: E402
from http_client import PageResult, ResilientHTTPClient  # noqa: E402


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


class HTTPRetryTests(unittest.TestCase):
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
