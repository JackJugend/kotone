"""Local PNG cards for SQLite statistics; this module never uses the network."""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


WIDTH = 1200
HEIGHT = 675
BACKGROUND = (30, 31, 36)
PANEL = (45, 47, 54)
PANEL_ALT = (55, 57, 65)
TEXT = (242, 243, 245)
MUTED = (181, 186, 194)
BLUE = (88, 101, 242)
GREEN = (82, 196, 122)
GOLD = (245, 183, 66)


def _font(size: int, *, bold: bool = False):
    filename = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    candidates = (
        Path(__file__).with_name("assets") / filename,
        Path("/usr/share/fonts/truetype/dejavu") / filename,
        Path("C:/Windows/Fonts") / ("arialbd.ttf" if bold else "arial.ttf"),
    )
    for candidate in candidates:
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size=size)
    try:
        return ImageFont.truetype(filename, size=size)
    except OSError:
        return ImageFont.load_default()


FONT_18 = lambda: _font(18)
FONT_26_B = lambda: _font(26, bold=True)
FONT_34_B = lambda: _font(34, bold=True)


def _number(value, digits: int = 1) -> str:
    if value is None:
        return "—"
    return f"{float(value):.{digits}f}"


def _fit(draw: ImageDraw.ImageDraw, text: str, font, width: int) -> str:
    text = str(text or "—")
    if draw.textlength(text, font=font) <= width:
        return text
    suffix = "…"
    while text and draw.textlength(text + suffix, font=font) > width:
        text = text[:-1]
    return text + suffix


def _base(title: str, subtitle: str):
    image = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((34, 28, WIDTH - 34, HEIGHT - 28), 28, fill=PANEL)
    draw.rectangle((34, 28, 47, HEIGHT - 28), fill=BLUE)
    draw.text((78, 56), title, font=FONT_34_B(), fill=TEXT)
    draw.text((78, 103), subtitle, font=FONT_18(), fill=MUTED)
    return image, draw


def _metric(draw, x: int, y: int, width: int, label: str, value: str, color=TEXT):
    draw.rounded_rectangle((x, y, x + width, y + 98), 16, fill=PANEL_ALT)
    draw.text((x + 20, y + 15), label, font=FONT_18(), fill=MUTED)
    draw.text((x + 20, y + 43), value, font=FONT_34_B(), fill=color)


def _bars(draw, items, box, *, color=BLUE, label_width=150):
    x1, y1, x2, y2 = box
    rows = list(items)[:5]
    maximum = max((int(value) for _, value in rows), default=1) or 1
    row_height = max(38, (y2 - y1) // max(1, len(rows)))
    font = FONT_18()
    for index, (label, value) in enumerate(rows):
        y = y1 + index * row_height
        label_text = _fit(draw, str(label), font, label_width - 10)
        draw.text((x1, y + 7), label_text, font=font, fill=TEXT)
        bar_x = x1 + label_width
        bar_width = int((x2 - bar_x - 55) * int(value) / maximum)
        draw.rounded_rectangle(
            (bar_x, y + 10, max(bar_x + 4, bar_x + bar_width), y + 28),
            8,
            fill=color,
        )
        draw.text((x2 - 38, y + 6), str(value), font=font, fill=MUTED)


def _save(image: Image.Image) -> io.BytesIO:
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    output.seek(0)
    return output


def render_stats(data: dict) -> io.BytesIO:
    image, draw = _base(
        f"Kotone Stats • {data['username']}",
        "Wyłącznie lokalna baza SQLite • użytkownik z configu",
    )
    _metric(draw, 78, 150, 225, "Oceny", str(data["ratings"]), BLUE)
    _metric(draw, 323, 150, 225, "Średnia", _number(data["average"]), GREEN)
    _metric(draw, 568, 150, 225, "Mediana", _number(data["median"]), GOLD)
    _metric(draw, 813, 150, 225, "Track scores", str(data["track_scores"]), TEXT)

    draw.text((78, 288), "Rozkład ocen", font=FONT_26_B(), fill=TEXT)
    _bars(draw, data["score_buckets"], (78, 332, 535, 574), color=BLUE, label_width=105)

    draw.text((610, 288), "Najczęstsze gatunki", font=FONT_26_B(), fill=TEXT)
    genres = data["top_genres"] or [("Brak danych", 0)]
    _bars(draw, genres, (610, 332, 1080, 574), color=GREEN, label_width=205)
    draw.text(
        (78, 602),
        f"Recenzje {data['reviews']}  •  Likes {data['likes']}  •  Albumy z Track Ratings {data['track_albums']}",
        font=FONT_18(),
        fill=MUTED,
    )
    return _save(image)


def render_compare(data: dict) -> io.BytesIO:
    image, draw = _base(
        f"{data['user_a']}  ×  {data['user_b']}",
        "Porównanie wspólnych ocen zapisanych w SQLite",
    )
    _metric(draw, 78, 150, 225, "Wspólne", str(data["common_count"]), BLUE)
    _metric(draw, 323, 150, 225, data["user_a"], _number(data["average_a"]), GREEN)
    _metric(draw, 568, 150, 225, data["user_b"], _number(data["average_b"]), GREEN)
    _metric(draw, 813, 150, 225, "Zgodność", f"{_number(data['agreement'])}%", GOLD)

    draw.text((78, 288), "Największe różnice", font=FONT_26_B(), fill=TEXT)
    font = FONT_18()
    items = data["disagreements"] or []
    if not items:
        draw.text((78, 342), "Brak wspólnych ocen", font=font, fill=MUTED)
    for index, item in enumerate(items[:5], start=1):
        y = 326 + (index - 1) * 53
        name = _fit(draw, f"{item['artist']} — {item['album']}", font, 660)
        draw.text((78, y), f"{index}. {name}", font=font, fill=TEXT)
        scores = f"{item['score_a']:.0f}  /  {item['score_b']:.0f}   Δ {item['gap']:.0f}"
        draw.text((825, y), scores, font=font, fill=GOLD)
    return _save(image)


def render_wrapped(data: dict) -> io.BytesIO:
    image, draw = _base(
        f"Kotone Wrapped {data['year']} • {data['username']}",
        "Rok oceniania zapisany lokalnie w SQLite",
    )
    _metric(draw, 78, 150, 225, "Oceny", str(data["ratings"]), BLUE)
    _metric(draw, 323, 150, 225, "Średnia", _number(data["average"]), GREEN)
    _metric(draw, 568, 150, 225, "Recenzje", str(data["reviews"]), GOLD)
    _metric(draw, 813, 150, 225, "Likes", str(data["likes"]), TEXT)

    draw.text((78, 288), "Aktywność w miesiącach", font=FONT_26_B(), fill=TEXT)
    months = [(str(month), count) for month, count in data["months"]]
    maximum = max((count for _, count in months), default=1) or 1
    x_start, y_base = 78, 525
    bar_width, gap = 62, 18
    for index, (month, count) in enumerate(months):
        x = x_start + index * (bar_width + gap)
        height = int(165 * count / maximum)
        draw.rounded_rectangle(
            (x, y_base - height, x + bar_width, y_base),
            8,
            fill=BLUE if count else PANEL_ALT,
        )
        draw.text((x + 20, y_base + 12), month, font=FONT_18(), fill=MUTED)
        if count:
            draw.text((x + 21, y_base - height - 27), str(count), font=FONT_18(), fill=TEXT)
    return _save(image)
