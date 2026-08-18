"""Czytelne, deterministyczne karty PNG tworzone wyłącznie z danych SQLite."""

from __future__ import annotations

import io
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps


WIDTH = 1000
HEIGHT = 900
BACKGROUND = (30, 31, 36)
PANEL = (45, 47, 54)
PANEL_ALT = (55, 57, 65)
TEXT = (242, 243, 245)
MUTED = (190, 194, 202)
BLUE = (88, 101, 242)
GREEN = (82, 196, 122)
GOLD = (245, 183, 66)
RATING_COLORS = (
    (0, 224, 224),
    (0, 235, 167),
    (0, 225, 91),
    (35, 245, 24),
    (151, 245, 0),
    (246, 230, 0),
    (255, 164, 0),
    (255, 101, 0),
    (255, 55, 26),
    (225, 25, 25),
    (125, 12, 12),
)
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


def _score_color(value):
    try:
        score = int(float(value))
    except (TypeError, ValueError):
        return MUTED
    if score == 100:
        return (66, 255, 255)
    if score >= 90:
        return (28, 242, 155)
    if score >= 80:
        return (18, 215, 98)
    if score >= 70:
        return (51, 255, 0)
    if score >= 60:
        return (174, 255, 0)
    if score >= 50:
        return (255, 229, 0)
    if score >= 40:
        return (255, 157, 0)
    if score >= 30:
        return (255, 91, 0)
    if score >= 20:
        return (255, 31, 15)
    if score >= 10:
        return (140, 20, 20)
    return (88, 32, 32)


def _centered_x(draw: ImageDraw.ImageDraw, text: str, font, left: int, right: int) -> float:
    return left + (right - left - draw.textlength(text, font=font)) / 2


def _draw_parts(draw: ImageDraw.ImageDraw, parts, *, right: int, y: int, font) -> None:
    width = sum(draw.textlength(text, font=font) for text, _ in parts)
    x = right - width
    for text, color in parts:
        draw.text((x, y), text, font=font, fill=color)
        x += draw.textlength(text, font=font)


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
    title_font = _font(40, bold=True)
    subtitle_font = _font(22)
    draw.text(
        (_centered_x(draw, title, title_font, 54, 946), 48),
        title,
        font=title_font,
        fill=TEXT,
    )
    draw.text(
        (_centered_x(draw, subtitle, subtitle_font, 54, 946), 105),
        subtitle,
        font=subtitle_font,
        fill=MUTED,
    )
    return image, draw


def _avatar_badges(image: Image.Image, data: dict) -> None:
    avatars = list(data.get("_avatar_images") or [])[:2]
    for index, item in enumerate(reversed(avatars)):
        try:
            avatar = Image.open(io.BytesIO(item["image_bytes"])).convert("RGB")
            avatar = ImageOps.fit(avatar, (64, 64), method=Image.Resampling.LANCZOS)
        except Exception:
            continue
        mask = Image.new("L", (64, 64), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, 63, 63), fill=255)
        x = 902 - index * 52
        y = 45
        outline = Image.new("RGB", (70, 70), PANEL_ALT)
        outline_mask = Image.new("L", (70, 70), 0)
        ImageDraw.Draw(outline_mask).ellipse((0, 0, 69, 69), fill=255)
        image.paste(outline, (x - 3, y - 3), outline_mask)
        image.paste(avatar, (x, y), mask)


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
    label_font = _font(20)
    value_font = _font(38, bold=True)
    draw.text(
        (_centered_x(draw, label, label_font, x, x + width), y + 14),
        label,
        font=label_font,
        fill=MUTED,
    )
    draw.text(
        (_centered_x(draw, value, value_font, x, x + width), y + 48),
        value,
        font=value_font,
        fill=color,
    )


def _bars(
    draw: ImageDraw.ImageDraw,
    items,
    box,
    *,
    color=BLUE,
    colors=None,
    label_width=135,
    minimum_row_height=48,
    max_rows=5,
) -> None:
    x1, y1, x2, y2 = box
    rows = list(items)[:max_rows]
    maximum = max((int(value) for _, value in rows), default=1) or 1
    row_height = max(minimum_row_height, (y2 - y1) // max(1, len(rows)))
    font = _font(19 if minimum_row_height < 40 else 21)
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
            fill=(colors[index] if colors and index < len(colors) else color),
        )
        value_text = str(value)
        value_width = draw.textlength(value_text, font=font)
        draw.text((x2 - value_width, y + 5), value_text, font=font, fill=MUTED)


def _save(image: Image.Image) -> io.BytesIO:
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    output.seek(0)
    return output


def _cover_cards(draw: ImageDraw.ImageDraw, image: Image.Image, data: dict) -> None:
    """Add up to three compact cover cards from the persistent local cache."""

    covers = list(data.get("_cover_images") or [])[:3]
    if not covers:
        return
    y = 780
    card_width = 286
    for index, item in enumerate(covers):
        x = 54 + index * 302
        draw.rounded_rectangle((x, y, x + card_width, y + 88), 13, fill=PANEL_ALT)
        try:
            cover = Image.open(io.BytesIO(item["image_bytes"])).convert("RGB")
            cover = ImageOps.fit(cover, (72, 72), method=Image.Resampling.LANCZOS)
            mask = Image.new("L", (72, 72), 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, 71, 71), 9, fill=255)
            image.paste(cover, (x + 8, y + 8), mask)
        except Exception:
            continue

        title_font = _font(17, bold=True)
        detail_font = _font(15)
        title = _fit(
            draw,
            str(item.get("album") or "Nieznane wydanie"),
            title_font,
            card_width - 98,
        )
        artist = _fit(
            draw,
            str(item.get("artist") or "Nieznany artysta"),
            detail_font,
            card_width - 98,
        )
        score = _number(item.get("score", item.get("mean")), 0)
        draw.text((x + 92, y + 12), title, font=title_font, fill=TEXT)
        draw.text((x + 92, y + 38), artist, font=detail_font, fill=MUTED)
        draw.text(
            (x + 92, y + 61),
            f"Ocena: {score}",
            font=detail_font,
            fill=_score_color(item.get("score", item.get("mean"))),
        )


def render_stats(data: dict) -> io.BytesIO:
    image, draw = _base(
        f"Statystyki • {data['username']}",
        "Komenda bazuje na danych zapisanych przez bota",
    )
    _avatar_badges(image, data)
    _metric(draw, 54, "Liczba ocen", str(data["ratings"]), TEXT)
    _metric(draw, 283, "Średnia", _number(data["average"]), _score_color(data["average"]))
    _metric(draw, 512, "Mediana", _number(data["median"]), _score_color(data["median"]))
    _metric(draw, 741, "Ocenione tracklisty", str(data["track_albums"]), TEXT)

    draw.text((54, 302), "Rozkład ocen", font=_font(29, bold=True), fill=TEXT)
    _bars(
        draw,
        data["score_buckets"],
        (54, 346, 492, 730),
        colors=RATING_COLORS,
        label_width=92,
        minimum_row_height=34,
        max_rows=11,
    )

    draw.text((548, 302), "Najczęstsze gatunki", font=_font(29, bold=True), fill=TEXT)
    genres = data["top_genres"] or [("Brak danych", 0)]
    _bars(
        draw,
        genres,
        (548, 360, 938, 670),
        color=GREEN,
        label_width=190,
    )
    draw.text(
        (548, 695),
        (
            f"Recenzje: {data['reviews']}   •   Polubienia: {data['likes']}\n"
            f"Zapisane oceny utworów: {data['track_scores']}"
        ),
        font=_font(21),
        fill=MUTED,
    )
    _cover_cards(draw, image, data)
    return _save(image)


def render_rating_distribution(data: dict) -> io.BytesIO:
    """Render a chart-first AOTY-style score distribution."""

    image, draw = _base(
        f"Rozkład ocen • {data['username']}",
        str(data.get("filter_text") or data.get("label") or "Wszystko"),
    )
    _avatar_badges(image, data)

    _metric(draw, 162, "Liczba ocen", str(data["ratings"]), TEXT)
    _metric(
        draw,
        398,
        "Średnia",
        _number(data["average"]),
        _score_color(data["average"]),
    )
    _metric(
        draw,
        634,
        "Mediana",
        _number(data["median"]),
        _score_color(data["median"]),
    )

    heading = str(data.get("label") or "Wszystko")
    heading_font = _font(31, bold=True)
    draw.text(
        (_centered_x(draw, heading, heading_font, 70, 930), 306),
        heading,
        font=heading_font,
        fill=TEXT,
    )
    _bars(
        draw,
        data["score_buckets"],
        (96, 360, 904, 832),
        colors=RATING_COLORS,
        label_width=118,
        minimum_row_height=41,
        max_rows=11,
    )
    return _save(image)


def render_compare(data: dict) -> io.BytesIO:
    image, draw = _base(
        f"Porównanie • {data['user_a']} i {data['user_b']}",
        "Komenda bazuje na danych zapisanych przez bota",
    )
    _avatar_badges(image, data)
    _metric(draw, 54, "Wspólne oceny", str(data["common_count"]), TEXT)
    _metric(
        draw, 283, f"Średnia: {data['user_a']}", _number(data["average_a"]),
        _score_color(data["average_a"]),
    )
    _metric(
        draw, 512, f"Średnia: {data['user_b']}", _number(data["average_b"]),
        _score_color(data["average_b"]),
    )
    _metric(
        draw, 741, "Zgodność", f"{_number(data['agreement'])}%",
        _score_color(data["agreement"]),
    )

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
        _draw_parts(
            draw,
            (
                (f"{item['score_a']:.0f}", _score_color(item["score_a"])),
                (" / ", MUTED),
                (f"{item['score_b']:.0f}", _score_color(item["score_b"])),
                (f"   różnica: {item['gap']:.0f}", MUTED),
            ),
            right=938,
            y=y,
            font=font,
        )

    draw.text(
        (62, 730),
        "Zgodność = 100 minus średnia bezwzględna różnica ocen.",
        font=_font(21),
        fill=MUTED,
    )
    _cover_cards(draw, image, data)
    return _save(image)


def render_wrapped(data: dict) -> io.BytesIO:
    image, draw = _base(
        f"Podsumowanie {data['year']} • {data['username']}",
        "Komenda bazuje na danych zapisanych przez bota",
    )
    _avatar_badges(image, data)
    _metric(draw, 54, "Liczba ocen", str(data["ratings"]), TEXT)
    _metric(
        draw, 283, "Średnia", _number(data["average"]),
        _score_color(data["average"]),
    )
    _metric(draw, 512, "Recenzje", str(data["reviews"]), TEXT)
    _metric(draw, 741, "Polubienia", str(data["likes"]), TEXT)

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
    _cover_cards(draw, image, data)
    return _save(image)
