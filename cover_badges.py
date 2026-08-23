"""Deterministic cover decorations shared by Discord and chart graphics."""

from __future__ import annotations

import io
import math

from PIL import Image, ImageDraw


# AOTY ``.mustHear.user`` uses rgba(233, 116, 81, .85).  The generated
# Discord cover cannot keep the source image's alpha compositing reliably, so
# use its opaque base colour for the same orange badge.
MUST_HEAR_ORANGE = (233, 116, 81)
MUST_HEAR_BLUE = (98, 188, 250)
MUST_HEAR_PURPLE = (191, 64, 191)
BADGE_INK = (45, 47, 54)


def add_must_hear_badge(cover: Image.Image, *, kind: str = "users") -> Image.Image:
    """Add an AOTY-style Must Hear corner for users, critics or both."""

    cover = cover.copy().convert("RGB")
    draw = ImageDraw.Draw(cover)
    size = min(cover.size)
    # Discord mocno zmniejsza miniatury. Narożnik zajmuje 30% krótszego boku,
    # dzięki czemu kolor oraz gwiazda pozostają czytelne także przy małych
    # okładkach, ale nadal nie zasłaniają ich głównej części.
    corner = max(28, int(size * 0.30))
    width = cover.width
    color = {
        "users": MUST_HEAR_ORANGE,
        "critics": MUST_HEAR_BLUE,
        "both": MUST_HEAR_PURPLE,
    }.get(str(kind or "").casefold(), MUST_HEAR_ORANGE)
    draw.polygon(
        ((width - corner, 0), (width, 0), (width, corner)),
        fill=color,
    )

    center_x = width - corner * 0.30
    center_y = corner * 0.29
    outer = max(7, corner * 0.18)
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


def render_must_hear_png(content: bytes, *, kind: str = "users") -> bytes:
    image = Image.open(io.BytesIO(content)).convert("RGB")
    image = add_must_hear_badge(image, kind=kind)
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()
