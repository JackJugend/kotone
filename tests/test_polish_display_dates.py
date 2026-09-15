"""Polish presentation dates preserve absolute instants and date-only imports."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from bs4 import BeautifulSoup

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="kotone-polish-dates-"))

import aoty  # noqa: E402
from commands.profile import _lastfm_timestamp, _rating_date_text  # noqa: E402
from rating_import import parse_aoty_ratings_csv  # noqa: E402
from time_utils import POLISH_TIMEZONE, polish_datetime  # noqa: E402


class PolishDisplayDateTests(unittest.TestCase):
    def test_profile_timestamp_uses_polish_calendar_in_winter_and_summer(self):
        for instant, expected in (
            (datetime(2026, 1, 1, 23, 30, tzinfo=timezone.utc), "02.01.2026"),
            (datetime(2026, 9, 14, 22, 30, tzinfo=timezone.utc), "15.09.2026"),
        ):
            for multiplier in (1, 1000):
                with self.subTest(instant=instant, multiplier=multiplier):
                    self.assertEqual(
                        _rating_date_text({"sort_timestamp": instant.timestamp() * multiplier}),
                        expected,
                    )

    def test_csv_date_is_not_shifted_by_synthetic_end_of_day_timestamp(self):
        payload = (
            'Artist,Album,Year,Type,Rating,"Date Rated"\n'
            "Artist,Album,2026,LP,80,2026-08-17\n"
        ).encode("utf-8")
        row = parse_aoty_ratings_csv(payload)["rows"][0]
        self.assertEqual(polish_datetime(row["sort_timestamp"]).day, 18)
        self.assertEqual(_rating_date_text(row), "17.08.2026")

    def test_lastfm_absolute_time_is_polish_in_winter_and_summer(self):
        for instant, expected in (
            (datetime(2026, 1, 1, 23, 30, tzinfo=timezone.utc), "02.01.2026 00:30 CET"),
            (datetime(2026, 9, 14, 22, 30, tzinfo=timezone.utc), "15.09.2026 00:30 CEST"),
        ):
            with self.subTest(instant=instant):
                timestamp = int(instant.timestamp())
                self.assertEqual(_lastfm_timestamp(timestamp), f"\n{expected}  •  <t:{timestamp}:R>")

    def test_lastfm_absolute_time_handles_both_dst_transitions(self):
        for instant, expected in (
            (datetime(2026, 3, 29, 0, 30, tzinfo=timezone.utc), "29.03.2026 01:30 CET"),
            (datetime(2026, 3, 29, 1, 30, tzinfo=timezone.utc), "29.03.2026 03:30 CEST"),
            (datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc), "25.10.2026 02:30 CEST"),
            (datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc), "25.10.2026 02:30 CET"),
        ):
            with self.subTest(instant=instant):
                timestamp = int(instant.timestamp())
                self.assertEqual(_lastfm_timestamp(timestamp), f"\n{expected}  •  <t:{timestamp}:R>")

    def test_relative_dates_and_new_year_use_polish_today(self):
        now = datetime(2025, 12, 31, 23, 30, tzinfo=timezone.utc).astimezone(POLISH_TIMEZONE)
        with patch.object(aoty, "polish_now", return_value=now):
            self.assertEqual(aoty.format_polish_date("just now"), "01.01.2026")
            self.assertEqual(aoty.format_polish_date("1h ago"), "31.12.2025")
            self.assertEqual(aoty.format_polish_date("Jan 1"), "01.01.2026")
            self.assertEqual(aoty.format_polish_date("Dec 31"), "31.12.2025")
            self.assertEqual(aoty.format_polish_date("Sep 14, 2024"), "14.09.2024")

    def test_relative_date_uses_elapsed_hours_across_spring_dst_change(self):
        now = datetime(2026, 3, 29, 1, 30, tzinfo=timezone.utc).astimezone(POLISH_TIMEZONE)
        with patch.object(aoty, "polish_now", return_value=now):
            self.assertEqual(aoty.format_polish_date("3h ago"), "28.03.2026")

    def test_relative_sort_timestamp_remains_the_same_utc_instant(self):
        now = datetime(2026, 3, 29, 1, 30, tzinfo=timezone.utc)
        with patch.object(aoty, "datetime", wraps=datetime) as parser_datetime:
            parser_datetime.now.return_value = now
            parsed = aoty._parse_rating_datetime_for_sort("3h ago")
            parser_datetime.now.assert_called_once_with(timezone.utc)
        self.assertEqual(parsed.timestamp(), (now - timedelta(hours=3)).timestamp())

    def test_absolute_aoty_date_keeps_utc_ordering_anchor(self):
        parsed = aoty._parse_rating_datetime_for_sort("Sep 14, 2026")
        self.assertEqual(parsed, datetime(2026, 9, 14, tzinfo=timezone.utc))

    def test_missing_rating_date_uses_polish_today(self):
        block = BeautifulSoup(
            '<div><a class="albumTitle" href="/album/123-album.php">Album</a>'
            '<div class="rating">80</div></div>',
            "html.parser",
        ).div
        now = datetime(2025, 12, 31, 23, 30, tzinfo=timezone.utc).astimezone(POLISH_TIMEZONE)
        with patch.object(aoty, "polish_now", return_value=now):
            self.assertEqual(aoty.parse_album_block(block)["date"], "01.01.2026")


if __name__ == "__main__":
    unittest.main()
