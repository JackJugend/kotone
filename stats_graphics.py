"""Czytelne, deterministyczne karty PNG tworzone wyłącznie z danych SQLite."""

from __future__ import annotations

import io
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from cover_badges import add_must_hear_badge


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
USER_A_COLOR = (167, 139, 250)
USER_B_COLOR = (45, 212, 191)
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


def _base(title: str, subtitle: str, *, height: int = HEIGHT):
    image = Image.new("RGB", (WIDTH, height), BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((24, 24, WIDTH - 24, height - 24), 26, fill=PANEL)
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


def _must_hear_cover_badge(cover: Image.Image, item: dict) -> Image.Image:
    """Add only the orange AOTY Must Hear corner to a rendered cover."""

    if not item.get("must_hear"):
        return cover
    return add_must_hear_badge(cover)


def _cover_cards(
    draw: ImageDraw.ImageDraw,
    image: Image.Image,
    data: dict,
    *,
    y: int = 780,
) -> None:
    """Add up to three compact cover cards from the persistent local cache."""

    covers = list(data.get("_cover_images") or [])[:3]
    if not covers:
        return
    card_width = 286
    for index, item in enumerate(covers):
        x = 54 + index * 302
        draw.rounded_rectangle((x, y, x + card_width, y + 88), 13, fill=PANEL_ALT)
        try:
            cover = Image.open(io.BytesIO(item["image_bytes"])).convert("RGB")
            cover = ImageOps.fit(cover, (72, 72), method=Image.Resampling.LANCZOS)
            cover = _must_hear_cover_badge(cover, item)
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
        height=1220,
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
        (96, 360, 904, 810),
        colors=RATING_COLORS,
        label_width=118,
        minimum_row_height=41,
        max_rows=11,
    )

    example_images = list(data.get("_example_images") or [])

    def example_panel(x1: int, title: str, examples: list[dict]) -> None:
        x2 = x1 + 422
        draw.rounded_rectangle((x1, 842, x2, 1184), 18, fill=PANEL_ALT)
        title_font = _font(23, bold=True)
        draw.text(
            (_centered_x(draw, title, title_font, x1, x2), 862),
            title,
            font=title_font,
            fill=TEXT,
        )
        if not examples:
            empty = "Brak pozycji dla tych filtrów"
            font = _font(19)
            draw.text(
                (_centered_x(draw, empty, font, x1, x2), 955),
                empty,
                font=font,
                fill=MUTED,
            )
            return

        card_width = 186
        for index, item in enumerate(examples[:2]):
            card_x = x1 + 17 + index * 202
            cover_y = 908
            cover_item = next(
                (
                    candidate
                    for candidate in example_images
                    if candidate.get("album_id") == item.get("album_id")
                    and candidate.get("title") == item.get("title")
                ),
                None,
            )
            cover_size = 150
            cover_loaded = False
            if cover_item:
                try:
                    cover = Image.open(
                        io.BytesIO(cover_item["image_bytes"])
                    ).convert("RGB")
                    cover = ImageOps.fit(
                        cover,
                        (cover_size, cover_size),
                        method=Image.Resampling.LANCZOS,
                    )
                    cover = _must_hear_cover_badge(cover, item)
                    mask = Image.new("L", (cover_size, cover_size), 0)
                    ImageDraw.Draw(mask).rounded_rectangle(
                        (0, 0, cover_size - 1, cover_size - 1), 10, fill=255
                    )
                    image.paste(cover, (card_x + 18, cover_y), mask)
                    cover_loaded = True
                except Exception:
                    pass

            if not cover_loaded:
                draw.rounded_rectangle(
                    (
                        card_x + 18,
                        cover_y,
                        card_x + 18 + cover_size,
                        cover_y + cover_size,
                    ),
                    10,
                    fill=(66, 68, 77),
                )
                placeholder = "♪"
                placeholder_font = _font(54, bold=True)
                draw.text(
                    (
                        _centered_x(
                            draw,
                            placeholder,
                            placeholder_font,
                            card_x + 18,
                            card_x + 18 + cover_size,
                        ),
                        cover_y + 42,
                    ),
                    placeholder,
                    font=placeholder_font,
                    fill=MUTED,
                )

            title_text = _fit(
                draw,
                str(item.get("title") or "Nieznana pozycja"),
                _font(18, bold=True),
                card_width - 8,
            )
            artist = _fit(
                draw,
                str(item.get("artist") or "Nieznany artysta"),
                _font(16),
                card_width - 8,
            )
            draw.text(
                (card_x + 4, cover_y + 166),
                title_text,
                font=_font(18, bold=True),
                fill=TEXT,
            )
            draw.text(
                (card_x + 4, cover_y + 197),
                artist,
                font=_font(16),
                fill=MUTED,
            )
            score = _number(item.get("score"), 0)
            score_font = _font(24, bold=True)
            score_width = draw.textlength(score, font=score_font)
            draw.rounded_rectangle(
                (
                    card_x + 166 - score_width,
                    cover_y + cover_size - 42,
                    card_x + 178,
                    cover_y + cover_size - 8,
                ),
                9,
                fill=(32, 33, 38),
            )
            draw.text(
                (
                    card_x + 172 - score_width,
                    cover_y + cover_size - 41,
                ),
                score,
                font=score_font,
                fill=_score_color(item.get("score")),
            )

    example_panel(54, "Najwyżej ocenione", data.get("best_examples") or [])
    example_panel(524, "Najniżej ocenione", data.get("worst_examples") or [])
    return _save(image)


def render_compare(data: dict) -> io.BytesIO:
    compare_height = 1120 if data.get("_cover_images") else 990
    image, draw = _base(
        f"Porównanie • {data['user_a']} i {data['user_b']}",
        "Komenda bazuje na danych zapisanych przez bota",
        height=compare_height,
    )
    _avatar_badges(image, data)

    def user_card(x1: int, username: str, average, median, ratings: int, color) -> None:
        x2 = x1 + 434
        draw.rounded_rectangle((x1, 154, x2, 306), 18, fill=PANEL_ALT)
        name_font = _font(27, bold=True)
        draw.text(
            (_centered_x(draw, username, name_font, x1, x2), 172),
            username,
            font=name_font,
            fill=color,
        )
        average_text = _number(average)
        average_font = _font(45, bold=True)
        draw.text(
            (_centered_x(draw, average_text, average_font, x1, x2), 207),
            average_text,
            font=average_font,
            fill=_score_color(average),
        )
        detail = f"{ratings} ocen  •  mediana {_number(median)}"
        detail_font = _font(19)
        draw.text(
            (_centered_x(draw, detail, detail_font, x1, x2), 269),
            detail,
            font=detail_font,
            fill=MUTED,
        )

    user_card(
        54, data["user_a"], data["average_a"], data["median_a"],
        data["ratings_a"], USER_A_COLOR,
    )
    user_card(
        512, data["user_b"], data["average_b"], data["median_b"],
        data["ratings_b"], USER_B_COLOR,
    )

    summary = (
        f"{data['common_count']} wspólnych ocen  •  "
        f"zgodność {_number(data['agreement'])}%"
    )
    summary_font = _font(22, bold=True)
    draw.text(
        (_centered_x(draw, summary, summary_font, 54, 946), 326),
        summary,
        font=summary_font,
        fill=TEXT,
    )

    def comparison_bars(
        y: int,
        label: str,
        value_a,
        value_b,
        maximum: float,
    ) -> None:
        label_font = _font(21, bold=True)
        value_font = _font(18, bold=True)
        bar_x1, bar_x2 = 216, 892
        draw.text(
            (_centered_x(draw, label, label_font, bar_x1, bar_x2), y),
            label,
            font=label_font,
            fill=TEXT,
        )
        for offset, value, color, username in (
            (32, value_a, USER_A_COLOR, data["user_a"]),
            (65, value_b, USER_B_COLOR, data["user_b"]),
        ):
            numeric = float(value or 0)
            draw.text((54, y + offset), username, font=_font(17), fill=color)
            draw.rounded_rectangle(
                (bar_x1, y + offset + 4, bar_x2, y + offset + 25),
                10,
                fill=(65, 67, 76),
            )
            width = int((bar_x2 - bar_x1) * numeric / max(1.0, maximum))
            if width:
                draw.rounded_rectangle(
                    (bar_x1, y + offset + 4, bar_x1 + max(5, width), y + offset + 25),
                    10,
                    fill=color,
                )
            value_text = _number(value, 0 if label == "Liczba ocen" else 1)
            draw.text((904, y + offset), value_text, font=value_font, fill=TEXT)

    comparison_bars(
        374, "Liczba ocen", data["ratings_a"], data["ratings_b"],
        max(data["ratings_a"], data["ratings_b"], 1),
    )
    comparison_bars(474, "Średnia ocen", data["average_a"], data["average_b"], 100)
    comparison_bars(574, "Mediana ocen", data["median_a"], data["median_b"], 100)

    def advantage_panel(x1: int, username: str, items: list[dict], side: str) -> None:
        x2 = x1 + 434
        draw.rounded_rectangle((x1, 690, x2, 948), 18, fill=PANEL_ALT)
        heading = f"{username} ocenia wyżej"
        heading_font = _font(23, bold=True)
        draw.text(
            (_centered_x(draw, heading, heading_font, x1, x2), 712),
            heading,
            font=heading_font,
            fill=USER_A_COLOR if side == "a" else USER_B_COLOR,
        )
        if not items:
            empty = "Brak wyżej ocenionych wspólnych pozycji"
            empty_font = _font(17)
            draw.text(
                (_centered_x(draw, empty, empty_font, x1, x2), 815),
                empty,
                font=empty_font,
                fill=MUTED,
            )
            return
        for index, item in enumerate(items[:3]):
            y = 758 + index * 60
            name = _fit(
                draw,
                f"{item['artist']} — {item['album']}",
                _font(17, bold=True),
                280,
            )
            draw.text((x1 + 18, y), name, font=_font(17, bold=True), fill=TEXT)
            score_a = item["score_a"]
            score_b = item["score_b"]
            parts = (
                (f"{data['user_a']} ", USER_A_COLOR),
                (f"{score_a:.0f}", _score_color(score_a)),
                ("  •  ", MUTED),
                (f"{data['user_b']} ", USER_B_COLOR),
                (f"{score_b:.0f}", _score_color(score_b)),
                (f"  •  +{item['gap']:.0f}", TEXT),
            )
            parts_font = _font(14, bold=True)
            part_x = x1 + 18
            for text, color in parts:
                draw.text((part_x, y + 27), text, font=parts_font, fill=color)
                part_x += draw.textlength(text, font=parts_font)

    advantage_panel(54, data["user_a"], data.get("ahead_a") or [], "a")
    advantage_panel(512, data["user_b"], data.get("ahead_b") or [], "b")
    if data.get("_cover_images"):
        _cover_cards(draw, image, data, y=980)
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
