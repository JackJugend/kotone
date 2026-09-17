"""Small visual details shared by the bottom cards on generated graphics."""

from __future__ import annotations

import io
import unittest

from PIL import Image

from stats_graphics import PANEL, PANEL_ALT, _cover_cards, render_stats


def cover_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (32, 32), (220, 20, 40)).save(output, format="PNG")
    return output.getvalue()


class DrawSpy:
    def __init__(self):
        self.texts = []

    def rounded_rectangle(self, *args, **kwargs):
        pass

    def textlength(self, value, *, font):
        return len(str(value)) * 8

    def text(self, position, value, **kwargs):
        self.texts.append(str(value))


class StatsCardLayoutTests(unittest.TestCase):
    def test_compact_card_shows_bare_score_without_ocena_prefix(self):
        draw = DrawSpy()
        image = Image.new("RGB", (1000, 900), PANEL)
        _cover_cards(draw, image, {
            "_cover_images": [{
                "image_bytes": cover_bytes(), "album": "Album", "artist": "Artist",
                "score": 100,
            }],
        })
        self.assertIn("100", draw.texts)
        self.assertFalse(any(text.startswith("Ocena:") for text in draw.texts))

    def test_stats_cards_are_raised_clear_of_the_bottom_edge(self):
        graphic = render_stats({
            "username": "enso", "ratings": 1, "average": 100, "median": 100,
            "track_albums": 0, "score_buckets": [("100", 1)],
            "top_genres": [], "_avatar_images": [],
            "_cover_images": [{
                "image_bytes": cover_bytes(), "album": "Album", "artist": "Artist",
                "score": 100,
            }],
        })
        with Image.open(graphic) as image:
            self.assertEqual(image.getpixel((70, 764)), PANEL_ALT)
            self.assertEqual(image.getpixel((70, 855)), PANEL)


if __name__ == "__main__":
    unittest.main()
