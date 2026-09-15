"""Visual regressions for /chart's absolute, stacked score distribution."""

from __future__ import annotations

import unittest

from PIL import Image

from stats_engine import SCORE_BUCKETS
from stats_graphics import PANEL, RATING_COLORS, render_chart


LABELS = [label for label, _, _ in SCORE_BUCKETS]


def plot_colors(image: Image.Image, box: tuple[int, int, int, int]) -> set[tuple[int, int, int]]:
    pixels = image.crop(box).tobytes()
    return set(zip(pixels[0::3], pixels[1::3], pixels[2::3]))


def chart_image(score_rows: list[dict[str, int]]) -> Image.Image:
    buckets = [
        {
            "start": f"2026-{index + 1:02}-01",
            "label": f"Okres {index + 1}",
            "count": sum(scores.values()),
            "score_counts": scores,
        }
        for index, scores in enumerate(score_rows)
    ]
    totals = [bucket["count"] for bucket in buckets]
    return Image.open(render_chart({
        "username": "enso", "chart_type": "monthly", "period": len(buckets),
        "buckets": buckets, "ratings": sum(totals),
        "average": sum(totals) / len(buckets), "peak": max(totals),
        "range_start": "2026-01-01", "range_end": "2026-09-15",
        "undated_ratings": 0, "current_period_incomplete": True,
    })).convert("RGB")


class ChartScoreColorTests(unittest.TestCase):
    def test_each_score_band_has_stats_color_and_low_scores_stack_at_bottom(self):
        image = chart_image([{label: 1 for label in LABELS}] * 2)
        self.assertEqual(image.size, (1000, 1000))
        # Eleven ratings produce a 0..15 axis. Sample band interiors rather
        # than legend swatches, grid lines or total-point decorations.
        for level, color in enumerate(reversed(RATING_COLORS)):
            y = round(806 - (level + 0.5) * 356 / 15)
            with self.subTest(score=LABELS[-level - 1]):
                self.assertEqual(image.getpixel((400, y)), color)

    def test_single_period_renders_real_stacked_column_with_all_score_colors(self):
        image = chart_image([{label: 1 for label in LABELS}])
        for level, color in enumerate(reversed(RATING_COLORS)):
            y = round(806 - (level + 0.5) * 356 / 15)
            with self.subTest(score=LABELS[-level - 1]):
                self.assertEqual(image.getpixel((534, y)), color)
        self.assertEqual(image.getpixel((650, 700)), PANEL)

    def test_area_height_preserves_absolute_counts_and_does_not_normalize(self):
        image = chart_image([{"80–89": 2}, {"80–89": 6}])
        # The same sole score group grows from 2 to 6; it must occupy less
        # height on the left rather than fill 100% at both calendar points.
        self.assertEqual(image.getpixel((200, 600)), PANEL)
        self.assertEqual(image.getpixel((200, 760)), RATING_COLORS[2])
        self.assertEqual(image.getpixel((800, 600)), RATING_COLORS[2])
        colors = plot_colors(image, (145, 451, 924, 806))
        for color in RATING_COLORS[:2] + RATING_COLORS[3:]:
            self.assertNotIn(color, colors)

    def test_zero_periods_have_no_phantom_colored_bands(self):
        for rows in ([{label: 0 for label in LABELS}], [{}, {}]):
            with self.subTest(periods=len(rows)):
                image = chart_image(rows)
                colors = plot_colors(image, (144, 450, 925, 807))
                for color in RATING_COLORS:
                    self.assertNotIn(color, colors)


if __name__ == "__main__":
    unittest.main()
