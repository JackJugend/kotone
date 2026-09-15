"""Calendar and saved-rating regressions for /chart's pure analytics."""

from __future__ import annotations

import copy
import unittest
from datetime import UTC, datetime, timedelta, timezone

from stats_engine import SCORE_BUCKETS, rating_activity


def rating(day: str, score="80", **extra) -> dict:
    return {"rating_date": day, "score": score, **extra}


class RatingActivityTests(unittest.TestCase):
    def test_months_include_current_month_and_empty_calendar_buckets(self):
        rows = [
            rating("31.12.2025"),
            rating("01.01.2026"),
            rating("28.02.2026"),
            rating("15.09.2026"),
            rating("30.11.2025"),
        ]
        data = rating_activity(
            "enso", rows, "monthly", 10, now=datetime(2026, 9, 15, tzinfo=UTC)
        )
        self.assertEqual(data["username"], "enso")
        self.assertEqual(data["chart_type"], "monthly")
        self.assertEqual(data["period"], 10)
        self.assertEqual(data["range_start"], "2025-12-01")
        self.assertEqual(data["range_end"], "2026-09-15")
        self.assertTrue(data["current_period_incomplete"])
        self.assertEqual(data["ratings"], 4)
        self.assertEqual(data["average"], 0.4)
        self.assertEqual(data["peak"], 1)
        self.assertEqual(data["undated_ratings"], 0)
        self.assertEqual(
            [bucket["count"] for bucket in data["buckets"]],
            [1, 1, 1, 0, 0, 0, 0, 0, 0, 1],
        )
        self.assertEqual(
            data["buckets"][-1],
            {
                "start": "2026-09-01", "end": "2026-10-01",
                "label": "Wrz 2026", "count": 1,
                "score_counts": {
                    label: int(label == "80–89") for label, _, _ in SCORE_BUCKETS
                },
            },
        )

    def test_months_have_exclusive_ends_and_real_leap_year_boundaries(self):
        data = rating_activity(
            "enso",
            [rating("31.01.2024"), rating("29.02.2024"), rating("01.03.2024")],
            "monthly", 3, now=datetime(2024, 3, 1),
        )
        self.assertEqual(
            [(bucket["start"], bucket["end"], bucket["count"]) for bucket in data["buckets"]],
            [
                ("2024-01-01", "2024-02-01", 1),
                ("2024-02-01", "2024-03-01", 1),
                ("2024-03-01", "2024-04-01", 1),
            ],
        )

    def test_daily_buckets_span_february_29_and_include_today(self):
        data = rating_activity(
            "enso",
            [rating("28.02.2024"), rating("29.02.2024"), rating("01.03.2024")],
            "daily", 3, now=datetime(2024, 3, 1),
        )
        self.assertEqual(
            [bucket["start"] for bucket in data["buckets"]],
            ["2024-02-28", "2024-02-29", "2024-03-01"],
        )
        self.assertEqual(data["buckets"][-1]["end"], "2024-03-02")
        self.assertEqual(data["buckets"][-1]["label"], "01.03.2024")
        self.assertEqual(data["ratings"], 3)

    def test_week_starts_on_monday_across_calendar_year(self):
        data = rating_activity(
            "enso",
            [
                rating("22.12.2024"), rating("23.12.2024"),
                rating("29.12.2024"), rating("30.12.2024"),
                rating("01.01.2025"), rating("02.01.2025"),
            ],
            "weekly", 2, now=datetime(2025, 1, 1),
        )
        self.assertEqual(
            [(bucket["start"], bucket["end"], bucket["count"]) for bucket in data["buckets"]],
            [("2024-12-23", "2024-12-30", 2), ("2024-12-30", "2025-01-06", 2)],
        )
        self.assertEqual(data["buckets"][-1]["label"], "30.12.2024")
        self.assertEqual(data["ratings"], 4)
        self.assertEqual(data["peak"], 2)

    def test_sunday_is_in_previous_bucket_when_now_is_monday(self):
        data = rating_activity(
            "enso", [rating("14.09.2026"), rating("13.09.2026")],
            "weekly", 2, now=datetime(2026, 9, 14),
        )
        self.assertEqual([bucket["count"] for bucket in data["buckets"]], [1, 1])
        self.assertEqual(data["buckets"][-1]["start"], "2026-09-14")

    def test_years_are_calendar_years_including_january_first(self):
        data = rating_activity(
            "enso", [rating("31.12.2024"), rating("01.01.2025"), rating("01.01.2023")],
            "yearly", 2, now=datetime(2025, 1, 1),
        )
        self.assertEqual(
            [(bucket["start"], bucket["end"], bucket["label"], bucket["count"]) for bucket in data["buckets"]],
            [
                ("2024-01-01", "2025-01-01", "2024", 1),
                ("2025-01-01", "2026-01-01", "2025", 1),
            ],
        )

    def test_empty_chart_has_requested_zero_buckets_and_zero_metrics(self):
        data = rating_activity("enso", [], now=datetime(2026, 9, 15))
        self.assertEqual(len(data["buckets"]), 12)
        self.assertEqual(data["range_start"], "2025-10-01")
        self.assertEqual([bucket["count"] for bucket in data["buckets"]], [0] * 12)
        self.assertEqual((data["ratings"], data["average"], data["peak"]), (0, 0, 0))
        for chart_type in ("daily", "weekly", "monthly", "yearly"):
            with self.subTest(chart_type=chart_type):
                self.assertEqual(
                    len(rating_activity("enso", [], chart_type, 60, now=datetime(2026, 9, 15))["buckets"]),
                    60,
                )

    def test_numeric_zero_and_hundred_count_but_nr_invalid_and_track_scores_do_not(self):
        rows = [
            rating("15.09.2026", score)
            for score in ("0", 100, " 80 ", "NR", "", None, "nan", "inf", -1, 101)
        ]
        rows += [rating("15.09.2026", "95", _track_score=True)]
        data = rating_activity("enso", rows, "daily", 1, now=datetime(2026, 9, 15))
        self.assertEqual(data["ratings"], 3)
        self.assertEqual(data["peak"], 3)
        self.assertEqual(data["undated_ratings"], 0)

    def test_saved_date_timestamp_wins_over_conflicting_text_and_uses_utc(self):
        timestamp = datetime(2026, 9, 1, 0, 30, tzinfo=UTC).timestamp()
        data = rating_activity(
            "enso", [rating("31.08.2026", sort_timestamp=str(timestamp))],
            "monthly", 2, now=datetime(2026, 9, 15),
        )
        self.assertEqual([bucket["count"] for bucket in data["buckets"]], [0, 1])

    def test_missing_or_invalid_timestamps_fall_back_to_explicit_calendar_dates(self):
        rows = [
            rating("15.09.2026", sort_timestamp=value)
            for value in (None, 0, -1, "invalid", float("nan"), float("inf"), 1e200)
        ]
        rows += [
            rating("2026-09-15"), rating("September 15, 2026"),
            rating("Sep 15, 2026"), rating("Sept. 15 2026"),
            rating("september 15th, 2026"),
        ]
        data = rating_activity("enso", rows, "daily", 1, now=datetime(2026, 9, 15))
        self.assertEqual(data["ratings"], len(rows))
        self.assertEqual(data["undated_ratings"], 0)

    def test_unreliable_dates_are_reported_without_using_database_seen_times(self):
        rows = [
            rating(value, first_seen_at=datetime(2026, 9, 15, tzinfo=UTC).timestamp())
            for value in (
                "", None, "Brak danych", "2 days ago", "just now", "Sep 15",
                "2026", "31.02.2026", "February 29, 2025", "not a date",
            )
        ]
        rows.append(rating("", "NR"))
        data = rating_activity("enso", rows, "monthly", 1, now=datetime(2026, 9, 15))
        self.assertEqual(data["ratings"], 0)
        self.assertEqual(data["undated_ratings"], 10)

    def test_future_dates_are_excluded_even_when_text_is_past(self):
        rows = [
            rating("16.09.2026"),
            rating("15.09.2026", sort_timestamp=datetime(2026, 9, 16, tzinfo=UTC).timestamp()),
            rating("15.09.2026", sort_timestamp=datetime(2026, 9, 15, 23, 59, tzinfo=UTC).timestamp()),
        ]
        data = rating_activity(
            "enso", rows, "daily", 1, now=datetime(2026, 9, 15, 12, tzinfo=UTC)
        )
        self.assertEqual(data["ratings"], 1)
        self.assertEqual(data["undated_ratings"], 0)

    def test_naive_now_is_utc_and_aware_now_is_converted_to_utc(self):
        naive = datetime(2026, 9, 15, 23)
        utc = naive.replace(tzinfo=UTC)
        local = datetime(2026, 9, 16, 1, tzinfo=timezone(timedelta(hours=2)))
        rows = [rating("15.09.2026"), rating("16.09.2026")]
        expected = rating_activity("enso", rows, "daily", 1, now=utc)
        self.assertEqual(rating_activity("enso", rows, "daily", 1, now=naive), expected)
        self.assertEqual(rating_activity("enso", rows, "daily", 1, now=local), expected)
        self.assertEqual(expected["range_end"], "2026-09-15")
        self.assertEqual(expected["ratings"], 1)

    def test_release_flags_nested_tracks_and_history_do_not_multiply_counts_or_mutate_rows(self):
        rows = [rating(
            "15.09.2026", "85", has_review=True, has_track_ratings=True,
            track_score_count=12, track_ratings=[{"score": "90"}] * 12,
            history=[{"score": "70"}, {"score": "85"}], liked=True,
        )]
        original = copy.deepcopy(rows)
        data = rating_activity("enso", rows, "daily", 1, now=datetime(2026, 9, 15))
        self.assertEqual(data["ratings"], 1)
        self.assertEqual(rows, original)

    def test_invalid_type_and_period_raise_value_error(self):
        for chart_type in (None, "Monthly", "hourly", "", 1, []):
            with self.subTest(chart_type=chart_type):
                with self.assertRaises(ValueError):
                    rating_activity("enso", [], chart_type=chart_type)
        for period in (None, 0, -1, 61, 1.5, 2.0, "10", True, False):
            with self.subTest(period=period):
                with self.assertRaises(ValueError):
                    rating_activity("enso", [], period=period)

    def test_score_layers_cover_every_boundary_and_sum_to_period_totals(self):
        rows = [rating("01.09.2026", str(score)) for score in range(101)]
        rows += [rating("01.08.2026", "0"), rating("01.08.2026", "100")]
        data = rating_activity("enso", rows, "monthly", 3, now=datetime(2026, 9, 15))
        empty, august, september = data["buckets"]
        self.assertEqual(empty["score_counts"], {label: 0 for label, _, _ in SCORE_BUCKETS})
        self.assertEqual(august["score_counts"]["0–9"], 1)
        self.assertEqual(august["score_counts"]["100"], 1)
        self.assertEqual(september["score_counts"]["100"], 1)
        for label, _, _ in SCORE_BUCKETS[1:]:
            self.assertEqual(september["score_counts"][label], 10)
        for bucket in data["buckets"]:
            self.assertEqual(sum(bucket["score_counts"].values()), bucket["count"])
        self.assertEqual(data["ratings"], 103)

    def test_fractional_scores_go_into_their_decade_and_invalid_rows_add_no_layers(self):
        rows = [rating("15.09.2026", score) for score in (9.9, 10.1, 89.5, 99.9, 100)]
        rows += [rating("15.09.2026", "NR"), rating("15.09.2026", 101)]
        rows += [rating("16.09.2026", 90), rating("", 90)]
        data = rating_activity("enso", rows, "daily", 1, now=datetime(2026, 9, 15))
        self.assertEqual(data["ratings"], 5)
        self.assertEqual(data["undated_ratings"], 1)
        self.assertEqual(
            {label: count for label, count in data["buckets"][0]["score_counts"].items() if count},
            {"0–9": 1, "10–19": 1, "80–89": 1, "90–99": 1, "100": 1},
        )


if __name__ == "__main__":
    unittest.main()
