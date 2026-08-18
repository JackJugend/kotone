"""Czytelne, deterministyczne karty PNG tworzone wyłącznie z danych SQLite."""

from __future__ import annotations

import io
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


WIDTH = 1000
HEIGHT = 820
BACKGROUND = (30, 31, 36)
PANEL = (45, 47, 54)
PANEL_ALT = (55, 57, 65)
TEXT = (242, 243, 245)
MUTED = (190, 194, 202)
BLUE = (88, 101, 242)
GREEN = (82, 196, 122)
GOLD = (245, 183, 66)
ASSETS = Path(__file__).with_name("assets")
MONTH_NAMES = (
    "Sty", "Lut", "Mar", "Kwi", "Maj", "Cze",
    "Lip", "Sie", "Wrz", "Paź", "Lis", "Gru",
)


@lru_cache(maxsize=32)
def _font(size: int, *, bold: bool = False):
    """Load the bundled Unicode font, identically locally and on Railway."""

    filename = "NotoSans-Bold.ttf" if bold else "NotoSans-Regular.ttf"
    path = ASSETS / filename
    if not path.is_file():
        raise RuntimeError(f"Brak dołączonego fontu statystyk: {filename}")
    return ImageFont.truetype(str(path), size=size)


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
    draw.rounded_rectangle((24, 24, WIDTH - 24, HEIGHT - 24), 26, fill=PANEL)
    draw.rectangle((24, 24, 36, HEIGHT - 24), fill=BLUE)
    draw.text((62, 48), title, font=_font(40, bold=True), fill=TEXT)
    draw.text((62, 105), subtitle, font=_font(22), fill=MUTED)
    return image, draw


def _metric(
    draw: ImageDraw.ImageDraw,
    x: int,
    label: str,
    value: str,
    color=TEXT,
) -> None:
    y = 158
    width = 205
    draw.rounded_rectangle((x, y, x + width, y + 112), 16, fill=PANEL_ALT)
    draw.text((x + 18, y + 14), label, font=_font(20), fill=MUTED)
    draw.text((x + 18, y + 48), value, font=_font(38, bold=True), fill=color)


def _bars(
    draw: ImageDraw.ImageDraw,
    items,
    box,
    *,
    color=BLUE,
    label_width=135,
) -> None:
    x1, y1, x2, y2 = box
    rows = list(items)[:5]
    maximum = max((int(value) for _, value in rows), default=1) or 1
    row_height = max(48, (y2 - y1) // max(1, len(rows)))
    font = _font(21)
    for index, (label, value) in enumerate(rows):
        y = y1 + index * row_height
        label_text = _fit(draw, str(label), font, label_width - 12)
        draw.text((x1, y + 7), label_text, font=font, fill=TEXT)
        bar_x = x1 + label_width
        available = x2 - bar_x - 48
        bar_width = int(available * int(value) / maximum)
        draw.rounded_rectangle(
            (bar_x, y + 12, max(bar_x + 4, bar_x + bar_width), y + 34),
            10,
            fill=color,
        )
        value_text = str(value)
        value_width = draw.textlength(value_text, font=font)
        draw.text((x2 - value_width, y + 5), value_text, font=font, fill=MUTED)


def _save(image: Image.Image) -> io.BytesIO:
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    output.seek(0)
    return output


def render_stats(data: dict) -> io.BytesIO:
    image, draw = _base(
        f"Statystyki • {data['username']}",
        "Dane zapisane w lokalnej bazie SQLite • bez zapytań sieciowych",
    )
    _metric(draw, 62, "Liczba ocen", str(data["ratings"]), BLUE)
    _metric(draw, 282, "Średnia", _number(data["average"]), GREEN)
    _metric(draw, 502, "Mediana", _number(data["median"]), GOLD)
    _metric(draw, 722, "Ocenione tracklisty", str(data["track_albums"]), TEXT)

    draw.text((62, 308), "Rozkład ocen", font=_font(29, bold=True), fill=TEXT)
    _bars(
        draw,
        data["score_buckets"],
        (62, 360, 455, 650),
        color=BLUE,
        label_width=105,
    )

    draw.text((535, 308), "Najczęstsze gatunki", font=_font(29, bold=True), fill=TEXT)
    genres = data["top_genres"] or [("Brak danych", 0)]
    _bars(
        draw,
        genres,
        (535, 360, 938, 650),
        color=GREEN,
        label_width=190,
    )
    draw.text(
        (62, 713),
        (
            f"Recenzje: {data['reviews']}   •   Polubienia: {data['likes']}   •   "
            f"Zapisane oceny utworów: {data['track_scores']}"
        ),
        font=_font(21),
        fill=MUTED,
    )
    return _save(image)


def render_compare(data: dict) -> io.BytesIO:
    image, draw = _base(
        f"Porównanie • {data['user_a']} i {data['user_b']}",
        "Wyłącznie wspólne oceny zapisane w lokalnej bazie SQLite",
    )
    _metric(draw, 62, "Wspólne oceny", str(data["common_count"]), BLUE)
    _metric(draw, 282, f"Średnia: {data['user_a']}", _number(data["average_a"]), GREEN)
    _metric(draw, 502, f"Średnia: {data['user_b']}", _number(data["average_b"]), GREEN)
    _metric(draw, 722, "Zgodność", f"{_number(data['agreement'])}%", GOLD)

    draw.text(
        (62, 308),
        "Największe różnice w ocenach",
        font=_font(29, bold=True),
        fill=TEXT,
    )
    font = _font(22)
    items = data["disagreements"] or []
    if not items:
        draw.text((62, 372), "Brak wspólnych ocen", font=font, fill=MUTED)
    for index, item in enumerate(items[:5], start=1):
        y = 366 + (index - 1) * 65
        name = _fit(draw, f"{item['artist']} — {item['album']}", font, 520)
        draw.text((62, y), f"{index}. {name}", font=font, fill=TEXT)
        scores = (
            f"{item['score_a']:.0f} / {item['score_b']:.0f}   "
            f"różnica: {item['gap']:.0f}"
        )
        scores_width = draw.textlength(scores, font=font)
        draw.text((938 - scores_width, y), scores, font=font, fill=GOLD)

    draw.text(
        (62, 730),
        "Zgodność = 100 minus średnia bezwzględna różnica ocen.",
        font=_font(21),
        fill=MUTED,
    )
    return _save(image)


def render_wrapped(data: dict) -> io.BytesIO:
    image, draw = _base(
        f"Podsumowanie {data['year']} • {data['username']}",
        "Rok dotyczy daty dodania oceny zapisanej w SQLite",
    )
    _metric(draw, 62, "Liczba ocen", str(data["ratings"]), BLUE)
    _metric(draw, 282, "Średnia", _number(data["average"]), GREEN)
    _metric(draw, 502, "Recenzje", str(data["reviews"]), GOLD)
    _metric(draw, 722, "Polubienia", str(data["likes"]), TEXT)

    draw.text(
        (62, 308),
        "Aktywność w kolejnych miesiącach",
        font=_font(29, bold=True),
        fill=TEXT,
    )
    maximum = max((count for _, count in data["months"]), default=1) or 1
    x_start, y_base = 62, 670
    bar_width, gap = 59, 17
    month_font = _font(18)
    value_font = _font(19, bold=True)
    for index, ((_, count), month_name) in enumerate(
        zip(data["months"], MONTH_NAMES)
    ):
        x = x_start + index * (bar_width + gap)
        height = int(245 * count / maximum)
        draw.rounded_rectangle(
            (x, y_base - height, x + bar_width, y_base),
            8,
            fill=BLUE if count else PANEL_ALT,
        )
        label_width = draw.textlength(month_name, font=month_font)
        draw.text(
            (x + (bar_width - label_width) / 2, y_base + 14),
            month_name,
            font=month_font,
            fill=MUTED,
        )
        if count:
            value = str(count)
            value_width = draw.textlength(value, font=value_font)
            draw.text(
                (x + (bar_width - value_width) / 2, y_base - height - 31),
                value,
                font=value_font,
                fill=TEXT,
            )
    return _save(image)
