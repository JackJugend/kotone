"""Czytelne, deterministyczne karty PNG tworzone wyłącznie z danych SQLite."""

from __future__ import annotations

import io
import math
from datetime import date
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from cover_badges import add_must_hear_badge
from stats_engine import SCORE_BUCKETS


WIDTH = 1000
HEIGHT = 900
CHART_HEIGHT = 950
CHART_WIDTH = 1500
CHART_FILL_ALPHA = 75
# Bazowa paleta interfejsu AOTY.  Wszystkie generowane wykresy korzystają z
# tych stałych, więc dalsze dostrojenie kolorów pozostaje w jednym miejscu.
BACKGROUND = (54, 57, 63)  # #202225
PANEL = (32, 34, 37)  # #36393f
PANEL_ALT = (47, 49, 54)  # #2f3136
TEXT = (242, 243, 245)
MUTED = (190, 194, 202)
BLUE = (88, 101, 242)
GREEN = (82, 196, 122)
GOLD = (245, 183, 66)
USER_A_COLOR = (167, 139, 250)
USER_B_COLOR = (45, 212, 191)
GENRE_COLORS = (
    (167, 139, 250),
    (151, 145, 251),
    (134, 151, 249),
    (117, 158, 246),
    (99, 166, 241),
    (81, 174, 234),
    (64, 183, 226),
    (50, 192, 215),
    (42, 202, 203),
    (45, 212, 191),
)
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


def _base(title: str, subtitle: str, *, height: int = HEIGHT, width: int = WIDTH):
    image = Image.new("RGB", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((24, 24, width - 24, height - 24), 26, fill=PANEL)
    title_font = _font(40, bold=True)
    subtitle_font = _font(22)
    draw.text(
        (_centered_x(draw, title, title_font, 54, width - 54), 48),
        title,
        font=title_font,
        fill=TEXT,
    )
    if subtitle:
        draw.text(
            (_centered_x(draw, subtitle, subtitle_font, 54, width - 54), 105),
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
        x = image.width - 98 - index * 52
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
    *,
    fit_text: bool = False,
    width: int = 205,
) -> None:
    y = 158
    draw.rounded_rectangle((x, y, x + width, y + 112), 16, fill=PANEL_ALT)
    label_font = _font(20)
    value_font = _font(38, bold=True)
    if fit_text:
        for size in range(20, 14, -1):
            label_font = _font(size)
            if draw.textlength(label, font=label_font) <= width - 20:
                break
        label = _fit(draw, label, label_font, width - 20)
        for size in range(38, 17, -1):
            value_font = _font(size, bold=True)
            if draw.textlength(value, font=value_font) <= width - 20:
                break
        value = _fit(draw, value, value_font, width - 20)
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

    section_font = _font(29, bold=True)
    distribution_heading = "Rozkład ocen"
    draw.text(
        (
            _centered_x(draw, distribution_heading, section_font, 54, 488),
            302,
        ),
        distribution_heading,
        font=section_font,
        fill=TEXT,
    )
    _bars(
        draw,
        data["score_buckets"],
        (54, 346, 488, 730),
        colors=RATING_COLORS,
        label_width=92,
        minimum_row_height=34,
        max_rows=11,
    )

    genres_heading = "Najczęstsze gatunki"
    draw.text(
        (
            _centered_x(draw, genres_heading, section_font, 512, 946),
            302,
        ),
        genres_heading,
        font=section_font,
        fill=TEXT,
    )
    genres = data["top_genres"] or [("Brak danych", 0)]
    _bars(
        draw,
        genres,
        (512, 346, 946, 730),
        colors=GENRE_COLORS,
        label_width=194,
        minimum_row_height=34,
        max_rows=10,
    )
    _cover_cards(draw, image, data)
    return _save(image)


def _chart_date(value) -> str:
    """Present calendar bucket boundaries in the same Polish date format."""

    try:
        return date.fromisoformat(str(value)[:10]).strftime("%d.%m.%Y")
    except (TypeError, ValueError):
        return str(value or "—")


def _chart_scale(maximum: int) -> tuple[int, int]:
    """Return an integer axis ceiling and a readable, non-fractional step."""

    if maximum <= 5:
        return max(1, maximum), 1
    target = maximum / 5
    magnitude = 10 ** math.floor(math.log10(target))
    step = next(
        int(multiplier * magnitude)
        for multiplier in (1, 2, 5, 10)
        if multiplier * magnitude >= target
    )
    return math.ceil(maximum / step) * step, step


def _chart_axis_number(value: int) -> str:
    """Keep a large integer axis readable without reducing the plotted data."""

    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:g} mld"
    if value >= 1_000_000:
        return f"{value / 1_000_000:g} mln"
    return f"{value:,}".replace(",", " ")


def _chart_curve(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Ease each segment while preserving counts and the order of stacked bands.

    Every boundary uses the same convex interpolation. Unlike independently
    fitted splines, it cannot overshoot, cross another band or invent ratings.
    """

    if len(points) < 2:
        return points
    curve = [points[0]]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        steps = max(2, math.ceil((x1 - x0) / 3))
        for step in range(1, steps + 1):
            t = step / steps
            eased = t * t * (3 - 2 * t)
            curve.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * eased))
    return curve


def _chart_areas(image, points, score_rows, score_labels, axis_maximum, box):
    """Composite translucent bands and rounded, antialiased outlines."""

    left, top, right, bottom = box
    scale, margin = 3, 10
    origin_x, origin_y = left - margin, top - margin
    size = ((right - left + margin * 2) * scale, (bottom - top + margin * 2) * scale)
    areas = Image.new("RGBA", size)
    outlines = Image.new("RGBA", size)
    area_draw, line_draw = ImageDraw.Draw(areas), ImageDraw.Draw(outlines)

    def scaled(curve):
        return [((x - origin_x) * scale, (y - origin_y) * scale) for x, y in curve]

    def rounded_line(curve, color, width):
        path = scaled(curve)
        line_draw.line(path, fill=color, width=width * scale, joint="curve")
        radius = width * scale / 2
        for x, y in (path[0], path[-1]):
            line_draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=color)

    cumulative = [0] * len(points)
    # Low scores form the bottom bands; a perfect 100 remains at the top.
    for label, color in reversed(list(zip(score_labels, RATING_COLORS))):
        values = [scores[label] for scores in score_rows]
        if not any(values):
            continue
        lower = [
            (x, bottom - total / axis_maximum * (bottom - top))
            for (x, _), total in zip(points, cumulative)
        ]
        cumulative = [total + value for total, value in zip(cumulative, values)]
        upper = [
            (x, bottom - total / axis_maximum * (bottom - top))
            for (x, _), total in zip(points, cumulative)
        ]
        if len(points) == 1:
            x = points[0][0]
            corners = scaled([(x - 28, upper[0][1]), (x + 28, lower[0][1])])
            area_draw.rectangle((*corners[0], *corners[1]), fill=(*color, CHART_FILL_ALPHA))
            rounded_line([(x - 28, upper[0][1]), (x + 28, upper[0][1])], (*color, 255), 3)
        else:
            upper_curve, lower_curve = _chart_curve(upper), _chart_curve(lower)
            area_draw.polygon(
                scaled([*upper_curve, *reversed(lower_curve)]),
                fill=(*color, CHART_FILL_ALPHA),
            )
            rounded_line(upper_curve, (*color, 255), 3)
    if len(points) == 1:
        x, y = points[0]
        mask = Image.new("L", size)
        corners = scaled([(x - 28, y), (x + 28, bottom)])
        ImageDraw.Draw(mask).rounded_rectangle((*corners[0], *corners[1]), 12 * scale, fill=255)
        for layer in (areas, outlines):
            layer.putalpha(Image.composite(layer.getchannel("A"), Image.new("L", size), mask))
    layer = Image.alpha_composite(areas, outlines).resize(
        (size[0] // scale, size[1] // scale), Image.Resampling.LANCZOS,
    )
    background = image.crop(
        (origin_x, origin_y, right + margin, bottom + margin),
    ).convert("RGBA")
    image.paste(
        Image.alpha_composite(background, layer).convert("RGB"),
        (origin_x, origin_y),
    )


def render_chart(data: dict) -> io.BytesIO:
    """Render absolute rating counts in colored, stacked score bands."""

    chart_type = str(data.get("chart_type") or "monthly")
    type_label, average_label, peak_label = {
        "daily": ("Dziennie", "Średnia / dzień", "Najwięcej / dzień"),
        "weekly": ("Tygodniowo", "Średnia / tydzień", "Najwięcej / tydzień"),
        "monthly": ("Miesięcznie", "Średnia / miesiąc", "Najwięcej / miesiąc"),
        "yearly": ("Rocznie", "Średnia / rok", "Najwięcej / rok"),
    }.get(chart_type, ("Miesięcznie", "Średnia / miesiąc", "Najwięcej / miesiąc"))
    buckets = list(data.get("buckets") or [])
    range_text = (
        f"{_chart_date(data.get('range_start'))} – "
        f"{_chart_date(data.get('range_end'))}"
    )
    image, draw = _base("", f"{type_label} • {range_text}", height=CHART_HEIGHT, width=CHART_WIDTH)
    title_font = _font(40, bold=True)
    title_right = image.width - (124 if data.get("_avatar_images") else 54)
    title = _fit(
        draw,
        f"Aktywność  •  {data.get('username') or 'Użytkownik'}",
        title_font,
        title_right - 54,
    )
    draw.text(
        (_centered_x(draw, title, title_font, 54, title_right), 48),
        title,
        font=title_font,
        fill=TEXT,
    )
    _avatar_badges(image, data)
    metric_width = (image.width - 108 - 3 * 24) // 4
    metric_step = metric_width + 24
    _metric(
        draw, 54, "Liczba ocen", str(data.get("ratings", 0)), TEXT,
        fit_text=True, width=metric_width,
    )
    _metric(
        draw, 54 + 3 * metric_step, "Średnia / okres", _number(data.get("average_score")),
        _score_color(data.get("average_score")),
        fit_text=True, width=metric_width,
    )
    _metric(
        draw, 54 + metric_step, average_label, _number(data.get("average", 0)),
        GENRE_COLORS[0], fit_text=True, width=metric_width,
    )
    _metric(
        draw, 54 + 2 * metric_step, peak_label, str(data.get("peak", 0)),
        GENRE_COLORS[-1], fit_text=True, width=metric_width,
    )

    #heading_font = _font(28, bold=True)
    legend_font = _font(25, bold=True)
    score_labels = [label for label, _, _ in SCORE_BUCKETS]
    for index, (label, color) in enumerate(zip(score_labels, RATING_COLORS)):
        x = 70 + (index % 6) * ((image.width - 124) // 6)
        y = 351 + (index // 6) * 36
        draw.rounded_rectangle((x, y + 5, x + 10, y + 33), 4, fill=color, outline="#ffffffcc")
        draw.text((x + 20, y), label, font=legend_font, fill=MUTED)
    incomplete = bool(data.get("current_period_incomplete", True))

    plot_left, plot_top, plot_right, plot_bottom = 144, 450, image.width - 76, 806
    score_rows = [
        {
            label: max(0, int((bucket.get("score_counts") or {}).get(label, 0)))
            for label in score_labels
        }
        for bucket in buckets
    ]
    counts = [
        sum(scores.values()) if "score_counts" in bucket else max(0, int(bucket.get("count", 0)))
        for bucket, scores in zip(buckets, score_rows)
    ]
    maximum = max(counts, default=0)
    axis_maximum, axis_step = _chart_scale(maximum)
    tick_font = _font(18)
    for value in range(0, axis_maximum + 1, axis_step):
        y = plot_bottom - (plot_bottom - plot_top) * value / axis_maximum
        draw.line((plot_left, y, plot_right, y), fill=(75, 78, 86), width=1)
        label = _chart_axis_number(value)
        label = _fit(draw, label, tick_font, plot_left - 70)
        draw.text(
            (plot_left - 17 - draw.textlength(label, font=tick_font), y - 13),
            label,
            font=tick_font,
            fill=MUTED,
        )
    draw.line(
        (plot_left, plot_bottom, plot_right, plot_bottom),
        fill=(136, 141, 151),
        width=1,
    )

    points = [
        (
            (plot_left + plot_right) / 2
            if len(counts) == 1
            else plot_left + index * (plot_right - plot_left) / (len(counts) - 1),
            plot_bottom - count / axis_maximum * (plot_bottom - plot_top),
        )
        for index, count in enumerate(counts)
    ]
    if maximum:
        _chart_areas(
            image, points, score_rows, score_labels, axis_maximum,
            (plot_left, plot_top, plot_right, plot_bottom),
        )
    if len(points) <= 12:
        count_font = _font(16, bold=True)
        available = 120 if len(points) <= 1 else int((plot_right - plot_left) / (len(points) - 1)) - 12
        for (x, y), count in zip(points[:-1], counts[:-1]):
            value = _fit(draw, f"{count:,}".replace(",", " "), count_font, available)
            label_y = y + 12 if y < plot_top + 32 else y - 29
            draw.text(
                (x - draw.textlength(value, font=count_font) / 2, label_y),
                value,
                font=count_font,
                fill=TEXT,
                stroke_width=1,
                stroke_fill=PANEL,
            )

    if points:
        x, y = points[-1]
        value_font = _font(18, bold=True)
        value = _fit(draw, f"{counts[-1]:,}".replace(",", " ") + ("*" if incomplete else ""), value_font, 172)
        value_width = draw.textlength(value, font=value_font)
        label_x = max(plot_left, min(plot_right - value_width - 20, x - value_width / 2 - 10))
        label_y = y + 16 if y < plot_top + 46 else y - 43
        if len(points) == 1:
            label_x = min(plot_right - value_width - 20, x + 43)
            label_y = max(plot_top, y - 16)
        draw.rounded_rectangle(
            (label_x, label_y, label_x + value_width + 20, label_y + 32),
            8,
            fill=PANEL_ALT,
        )
        draw.text((label_x + 10, label_y + 3), value, font=value_font, fill=TEXT)

    # Pick labels by their real width. Endpoints always stay visible, even for
    # 60 daily buckets, and the last point never shares a label with its neighbor.
    axis_font = _font(16)
    if buckets:
        axis_labels = [str(bucket.get("label") or _chart_date(bucket.get("start"))) for bucket in buckets]
        multiline = chart_type == "monthly" and len(buckets) <= 12
        label_lines = [label.rsplit(" ", 1) if multiline else [label] for label in axis_labels]
        largest_label = max(draw.textlength(line, font=axis_font) for lines in label_lines for line in lines)
        # Reserve enough horizontal space for the endpoints, which are inset
        # to keep their full dates inside the card.
        max_labels = max(2, int((plot_right - plot_left - largest_label / 2) / (largest_label + 22)) + 1)
        if multiline or len(buckets) <= max_labels:
            label_indices = list(range(len(buckets)))
        else:
            label_indices = sorted({round(index * (len(buckets) - 1) / (max_labels - 1)) for index in range(max_labels)})
        if not multiline:
            while len(label_indices) > 2:
                boxes = []
                for index in label_indices:
                    width = draw.textlength(axis_labels[index], font=axis_font)
                    left = max(54, min(image.width - 54 - width, points[index][0] - width / 2))
                    boxes.append((left, left + width))
                if all(previous[1] + 14 <= following[0] for previous, following in zip(boxes, boxes[1:])):
                    break
                max_labels = len(label_indices) - 1
                label_indices = sorted({round(index * (len(buckets) - 1) / (max_labels - 1)) for index in range(max_labels)})
        for index in label_indices:
            x = points[index][0]
            draw.line((x, plot_bottom + 4, x, plot_bottom + 10), fill=(136, 141, 151), width=1)
            for line_index, label in enumerate(label_lines[index]):
                width = draw.textlength(label, font=axis_font)
                label_x = max(54, min(image.width - 54 - width, x - width / 2))
                draw.text((label_x, plot_bottom + 16 + 21 * line_index), label, font=axis_font, fill=MUTED)

    if not maximum:
        empty_font = _font(24, bold=True)
        empty = "Brak ocen w wybranym okresie"
        empty_left = (plot_left + plot_right - 518) // 2
        empty_right = empty_left + 518
        draw.rounded_rectangle((empty_left, 601, empty_right, 663), 14, fill=PANEL_ALT)
        draw.text(
            (_centered_x(draw, empty, empty_font, empty_left, empty_right), 615),
            empty,
            font=empty_font,
            fill=MUTED,
        )

    footer_font = _font(17)
    if incomplete:
        draw.text(
            (54, 878),
            "* Bieżący okres jest niepełny — liczony do dziś.",
            font=footer_font,
            fill=MUTED,
        )
    undated = int(data.get("undated_ratings") or 0)
    if undated:
        note = _fit(
            draw,
            f"Pominięte oceny bez daty: {undated:,}".replace(",", " "),
            _font(15),
            image.width - 108,
        )
        draw.text((54, 914), note, font=_font(15), fill=MUTED)
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
        "",
        height=compare_height,
    )

    def user_card(x1: int, username: str, average, median, ratings: int, color) -> None:
        x2 = x1 + 434
        draw.rounded_rectangle((x1, 130, x2, 306), 18, fill=PANEL_ALT)
        avatar_item = next(
            (
                item
                for item in data.get("_avatar_images") or []
                if str(item.get("username") or "").casefold()
                == username.casefold()
            ),
            None,
        )
        if avatar_item:
            try:
                avatar = Image.open(
                    io.BytesIO(avatar_item["image_bytes"])
                ).convert("RGB")
                avatar = ImageOps.fit(
                    avatar,
                    (78, 78),
                    method=Image.Resampling.LANCZOS,
                )
                mask = Image.new("L", (78, 78), 0)
                ImageDraw.Draw(mask).ellipse((0, 0, 77, 77), fill=255)
                image.paste(avatar, (x1 + 24, 156), mask)
            except Exception:
                pass

        content_left = x1 + 112
        name_font = _font(34, bold=True)
        draw.text(
            (_centered_x(draw, username, name_font, content_left, x2 - 12), 148),
            username,
            font=name_font,
            fill=color,
        )
        average_text = _number(average)
        average_font = _font(45, bold=True)
        draw.text(
            (
                _centered_x(
                    draw, average_text, average_font, content_left, x2 - 12
                ),
                198,
            ),
            average_text,
            font=average_font,
            fill=_score_color(average),
        )
        detail = f"{ratings} ocen  •  mediana {_number(median)}"
        detail_font = _font(19)
        draw.text(
            (_centered_x(draw, detail, detail_font, x1, x2), 270),
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
        label_font = _font(24, bold=True)
        value_font = _font(22, bold=True)
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
            draw.text(
                (54, y + offset - 2),
                username,
                font=_font(22, bold=True),
                fill=color,
            )
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
        heading_font = _font(31, bold=True)
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
        for index, item in enumerate(items[:2]):
            y = 770 + index * 90
            name = _fit(
                draw,
                f"{item['artist']} — {item['album']}",
                _font(23, bold=True),
                392,
            )
            draw.text((x1 + 18, y), name, font=_font(23, bold=True), fill=TEXT)
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
            parts_font = _font(20, bold=True)
            part_x = x1 + 18
            for text, color in parts:
                draw.text((part_x, y + 37), text, font=parts_font, fill=color)
                part_x += draw.textlength(text, font=parts_font)

    advantage_panel(54, data["user_a"], data.get("ahead_a") or [], "a")
    advantage_panel(512, data["user_b"], data.get("ahead_b") or [], "b")
    if data.get("_cover_images"):
        _cover_cards(draw, image, data, y=980)
    return _save(image)


def render_wrapped(data: dict) -> io.BytesIO:
    image, draw = _base(
        f"Podsumowanie {data['year']} • {data['username']}",
        "",
        height=1400,
    )
    _avatar_badges(image, data)

    def summary_tile(x: int, y: int, label: str, value: str, color=TEXT) -> None:
        width = 270
        draw.rounded_rectangle((x, y, x + width, y + 96), 16, fill=PANEL_ALT)
        label_font = _font(18)
        value_font = _font(32, bold=True)
        draw.text(
            (_centered_x(draw, label, label_font, x, x + width), y + 12),
            label,
            font=label_font,
            fill=MUTED,
        )
        draw.text(
            (_centered_x(draw, value, value_font, x, x + width), y + 44),
            value,
            font=value_font,
            fill=color,
        )

    summary_tile(54, 130, "Liczba ocen", str(data["ratings"]))
    summary_tile(
        365, 130, "Średnia", _number(data["average"]),
        _score_color(data["average"]),
    )
    summary_tile(
        676, 130, "Mediana", _number(data["median"]),
        _score_color(data["median"]),
    )
    summary_tile(54, 246, "Recenzje", str(data["reviews"]))
    summary_tile(365, 246, "Polubienia", str(data["likes"]))
    summary_tile(676, 246, "Ocenione tracklisty", str(data["track_albums"]))

    section_font = _font(29, bold=True)
    activity_heading = "Aktywność w kolejnych miesiącach"
    draw.text(
        (_centered_x(draw, activity_heading, section_font, 54, 946), 382),
        activity_heading,
        font=section_font,
        fill=TEXT,
    )
    maximum = max((count for _, count in data["months"]), default=1) or 1
    x_start, y_base = 62, 720
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

    column_heading_font = _font(27, bold=True)
    genres_heading = "Gatunki roku"
    artists_heading = "Artyści roku"
    draw.text(
        (_centered_x(draw, genres_heading, column_heading_font, 54, 488), 790),
        genres_heading,
        font=column_heading_font,
        fill=TEXT,
    )
    draw.text(
        (_centered_x(draw, artists_heading, column_heading_font, 512, 946), 790),
        artists_heading,
        font=column_heading_font,
        fill=TEXT,
    )
    _bars(
        draw,
        (data.get("top_genres") or [("Brak danych", 0)])[:5],
        (54, 838, 488, 1118),
        colors=GENRE_COLORS,
        label_width=185,
        max_rows=5,
    )
    _bars(
        draw,
        (data.get("top_artists") or [("Brak danych", 0)])[:5],
        (512, 838, 946, 1118),
        colors=GENRE_COLORS[5:],
        label_width=190,
        max_rows=5,
    )
    draw.text(
        (
            _centered_x(
                draw,
                "Najwyżej ocenione",
                column_heading_font,
                54,
                946,
            ),
            1162,
        ),
        "Najwyżej ocenione",
        font=column_heading_font,
        fill=TEXT,
    )
    _cover_cards(draw, image, data, y=1220)
    return _save(image)
