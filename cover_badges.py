"""Deterministic cover decorations shared by Discord and chart graphics."""

from __future__ import annotations

import io
import math

from PIL import Image, ImageDraw


MUST_HEAR_ORANGE = (242, 126, 91)
BADGE_INK = (45, 47, 54)


def add_must_hear_badge(cover: Image.Image) -> Image.Image:
    """Add the orange AOTY-style corner and a dark five-point star."""

    cover = cover.copy().convert("RGB")
    draw = ImageDraw.Draw(cover)
    size = min(cover.size)
    corner = max(26, int(size * 0.34))
    width = cover.width
    draw.polygon(
        ((width - corner, 0), (width, 0), (width, corner)),
        fill=MUST_HEAR_ORANGE,
    )

    center_x = width - corner * 0.30
    center_y = corner * 0.29
    outer = max(6, corner * 0.16)
    inner = outer * 0.44
    points = []
    for index in range(10):
        angle = -math.pi / 2 + index * math.pi / 5
        radius = outer if index % 2 == 0 else inner
        points.append(
            (
                center_x + math.cos(angle) * radius,
                center_y + math.sin(angle) * radius,
            )
        )
    draw.polygon(points, fill=BADGE_INK)
    return cover


def render_must_hear_png(content: bytes) -> bytes:
    image = Image.open(io.BytesIO(content)).convert("RGB")
    image = add_must_hear_badge(image)
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()
