"""Visual regressions for /chart's absolute, stacked score distribution."""

from __future__ import annotations

import io
import unittest

from PIL import Image

from stats_engine import SCORE_BUCKETS
from stats_graphics import (
    CHART_FILL_ALPHA,
    CHART_HEIGHT,
    CHART_SMOOTH_POINT_LIMIT,
    CHART_WIDTH,
    PANEL,
    RATING_COLORS,
    TEXT,
    _chart_curve,
    render_chart,
)


LABELS = [label for label, _, _ in SCORE_BUCKETS]


def translucent(color: tuple[int, int, int]) -> tuple[int, int, int]:
    background_alpha = 255 - CHART_FILL_ALPHA
    return tuple(round((channel * CHART_FILL_ALPHA + background * background_alpha) / 255)
                 for channel, background in zip(color, PANEL))


def plot_colors(image: Image.Image, box: tuple[int, int, int, int]) -> set[tuple[int, int, int]]:
    pixels = image.crop(box).tobytes()
    return set(zip(pixels[0::3], pixels[1::3], pixels[2::3]))


def chart_image(
    score_rows: list[dict[str, int]],
    *,
    avatar_color: tuple[int, int, int] | None = None,
    bot_avatar_color: tuple[int, int, int] | None = None,
) -> Image.Image:
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
    avatar_images = []
    if avatar_color is not None:
        avatar_bytes = io.BytesIO()
        Image.new("RGB", (80, 80), avatar_color).save(avatar_bytes, "PNG")
        avatar_images.append({"image_bytes": avatar_bytes.getvalue()})
    bot_avatar_images = []
    if bot_avatar_color is not None:
        avatar_bytes = io.BytesIO()
        Image.new("RGB", (80, 80), bot_avatar_color).save(avatar_bytes, "PNG")
        bot_avatar_images.append({"image_bytes": avatar_bytes.getvalue()})
    return Image.open(render_chart({
        "username": "enso", "chart_type": "monthly", "period": len(buckets),
        "buckets": buckets, "ratings": sum(totals),
        "average": sum(totals) / len(buckets), "peak": max(totals),
        "range_start": "2026-01-01", "range_end": "2026-09-15",
        "undated_ratings": 0, "current_period_incomplete": True,
        "_avatar_images": avatar_images,
        "_bot_avatar_images": bot_avatar_images,
    })).convert("RGB")


class ChartScoreColorTests(unittest.TestCase):
    def assert_color(self, actual, expected):
        # Antialiasing premultiplied RGBA may round a channel by one unit.
        self.assertLessEqual(max(abs(a - b) for a, b in zip(actual, expected)), 1)

    def assert_closest_rating_color(self, actual, expected):
        distance = lambda color: sum(abs(a - b) for a, b in zip(actual, color))
        self.assertEqual(min(RATING_COLORS, key=distance), expected)

    def test_each_score_band_has_stats_color_and_low_scores_stack_at_bottom(self):
        image = chart_image([{label: 1 for label in LABELS}] * 2)
        self.assertEqual(image.size, (CHART_WIDTH, CHART_HEIGHT))
        # Eleven ratings produce a 0..15 axis. Sample band interiors rather
        # than legend swatches, grid lines or total-point decorations.
        for level, color in enumerate(reversed(RATING_COLORS)):
            y = round(858 - (level + 0.5) * 461 / 15)
            with self.subTest(score=LABELS[-level - 1]):
                self.assert_color(image.getpixel((400, y)), translucent(color))
        # Count labels sit at the endpoints; no white total line or markers
        # should cover the colored upper boundary through the plot's middle.
        self.assertNotIn(TEXT, plot_colors(image, (300, 398, 1200, 858)))

    def test_single_period_renders_real_stacked_column_with_all_score_colors(self):
        image = chart_image([{label: 1 for label in LABELS}])
        for level, color in enumerate(reversed(RATING_COLORS)):
            y = round(858 - (level + 0.5) * 461 / 15)
            with self.subTest(score=LABELS[-level - 1]):
                self.assert_color(image.getpixel((784, y)), translucent(color))
        # A single period fills the plot horizontally rather than becoming a
        # narrow central column.
        self.assert_color(image.getpixel((200, 700)), translucent(RATING_COLORS[5]))
        self.assert_color(image.getpixel((1350, 700)), translucent(RATING_COLORS[5]))

    def test_area_height_preserves_absolute_counts_and_does_not_normalize(self):
        image = chart_image([{"80–89": 2}, {"80–89": 6}])
        # The same sole score group grows from 2 to 6; it must occupy less
        # height on the left rather than fill 100% at both calendar points.
        self.assertEqual(image.getpixel((200, 600)), PANEL)
        self.assert_color(image.getpixel((200, 780)), translucent(RATING_COLORS[2]))
        self.assert_color(image.getpixel((1300, 600)), translucent(RATING_COLORS[2]))
        colors = plot_colors(image, (145, 398, 1424, 858))
        for color in RATING_COLORS[:2] + RATING_COLORS[3:]:
            self.assertNotIn(color, colors)

    def test_zero_periods_have_no_phantom_colored_bands(self):
        for rows in ([{label: 0 for label in LABELS}], [{}, {}]):
            with self.subTest(periods=len(rows)):
                image = chart_image(rows)
                colors = plot_colors(image, (144, 397, 1425, 859))
                for color in RATING_COLORS:
                    self.assertNotIn(color, colors)

    def test_area_uses_a_rounded_curve_instead_of_a_straight_diagonal(self):
        image = chart_image([{"80–89": 2}, {"80–89": 6}])
        # Near the low endpoint the curve rises gently; near the high endpoint
        # it levels off. A straight segment gives the opposite pixels here.
        self.assertEqual(image.getpixel((464, 645)), PANEL)
        self.assert_color(image.getpixel((1104, 460)), translucent(RATING_COLORS[2]))

    def test_visible_top_edge_uses_the_highest_score_present_at_each_period(self):
        image = chart_image([{"70–79": 5}, {"100": 5}, {"80–89": 5}])
        for x, color in (
            (144, RATING_COLORS[3]),
            (784, RATING_COLORS[0]),
            (1424, RATING_COLORS[2]),
        ):
            with self.subTest(x=x):
                self.assert_closest_rating_color(image.getpixel((x, 397)), color)

    def test_bot_and_user_avatars_are_symmetric_with_two_pixel_white_outlines(self):
        user_color = (213, 48, 139)
        bot_color = (75, 115, 220)
        image = chart_image(
            [{"70–79": 5}],
            avatar_color=user_color,
            bot_avatar_color=bot_color,
        )
        self.assertEqual(image.getpixel((79, 79)), bot_color)
        self.assertEqual(image.getpixel((1421, 79)), user_color)
        self.assertEqual(image.getpixel((79, 45)), (255, 255, 255))
        self.assertEqual(image.getpixel((1421, 45)), (255, 255, 255))

    def test_dense_series_uses_direct_interpolation_instead_of_flattened_spikes(self):
        points = [(index * 12, 100 if index else 0) for index in range(CHART_SMOOTH_POINT_LIMIT + 1)]
        curve = _chart_curve(points)
        self.assertEqual(curve[1], (3.0, 25.0))

    def test_legend_reads_left_to_right_in_one_row_with_space_below(self):
        image = chart_image([{"70–79": 5}])
        swatch_y = 320
        first_x_by_color = []
        for color in RATING_COLORS:
            matching = [x for x in range(54, CHART_WIDTH - 54) if image.getpixel((x, swatch_y)) == color]
            self.assertTrue(matching)
            first_x_by_color.append(matching[0])
        self.assertEqual(first_x_by_color, sorted(first_x_by_color))
        self.assertEqual(plot_colors(image, (54, 365, CHART_WIDTH - 54, 380)), {PANEL})


if __name__ == "__main__":
    unittest.main()
