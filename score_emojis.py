"""Deterministiczne emoji ocen 0–100 w stylu AOTY.

Każdy kafelek ma czarną podstawę, białą liczbę i pasek w aktualnej palecie
Kotone. Obraz powstaje lokalnie, więc AI ani zewnętrzna usługa nie mogą pomylić
cyfry oceny. Emoji są Application Emojis: należą do Kotone, nie do guildy.
"""

from __future__ import annotations

import asyncio
import base64
from io import BytesIO

import discord
from PIL import Image, ImageDraw, ImageFont

from database import DB
from score_emoji_registry import set_score_emojis
from status_emoji_registry import set_status_emojis

SCORE_EMOJI_PREFIX = "kotone_score_v2_"
SCORE_EMOJI_RENDER_VERSION = "aoty-tile-v2-transparent"
SCORE_EMOJI_SIZE = 96
SCORE_EMOJI_MAX_BYTES = 256 * 1024
STATUS_EMOJI_RENDER_VERSION = "aoty-flags-v1-transparent"
STATUS_EMOJI_NAMES = {
    "like": "kotone_like",
    "tracklist": "kotone_tracklist",
    "review": "kotone_review",
}


def score_emoji_name(score: int | None) -> str:
    """Use stable names so a restart never creates duplicate tiles."""

    return f"{SCORE_EMOJI_PREFIX}nr" if score is None else f"{SCORE_EMOJI_PREFIX}{int(score):03d}"


def _bar_color(score: int) -> tuple[int, int, int]:
    """Keep PNG colours in exact sync with shared.score_color thresholds."""

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
    return (58, 10, 10)


def _font(size: int) -> ImageFont.FreeTypeFont:
    """Use a Unicode-capable bundled font available on Railway and Windows."""

    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
    ):
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def render_score_emoji(score: int | None) -> bytes:
    """Render one compact, legible rating tile as a Discord-safe PNG."""

    value = None if score is None else int(score)
    if value is not None and not 0 <= value <= 100:
        raise ValueError("score emoji musi mieścić się w zakresie NR albo 0–100")

    size = SCORE_EMOJI_SIZE
    # Discord renders custom emoji over many themes. The canvas therefore
    # stays transparent; only the white glyphs receive a dark outline.
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    # Three digits need a little less space but remain more prominent than
    # the old circular markers.
    font = _font(46 if value is None or value < 100 else 37)
    text = "NR" if value is None else str(value)
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    width = right - left
    height = bottom - top
    draw.text(
        ((size - width) / 2 - left, 7 + (63 - height) / 2 - top),
        text,
        font=font,
        fill=(246, 247, 249, 255),
        stroke_width=3,
        stroke_fill=(0, 0, 0, 255),
    )

    if value is None:
        output = BytesIO()
        image.save(output, format="PNG", optimize=True)
        return output.getvalue()

    bar_left, bar_top, bar_right, bar_bottom = 10, 76, size - 10, 85
    draw.rounded_rectangle(
        (bar_left, bar_top, bar_right, bar_bottom),
        radius=4,
        fill=(0, 0, 0, 230),
    )
    filled_right = bar_left + round((bar_right - bar_left) * value / 100)
    if value > 0:
        draw.rounded_rectangle(
            (bar_left + 2, bar_top + 2, max(bar_left + 3, filled_right - 2), bar_bottom - 2),
            radius=4,
            fill=(*_bar_color(value), 255),
        )

    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    encoded = output.getvalue()
    if len(encoded) > SCORE_EMOJI_MAX_BYTES:
        raise ValueError("wygenerowane emoji oceny przekracza limit Discorda")
    return encoded


def _outlined_line(
    draw: ImageDraw.ImageDraw,
    points: list[tuple[int, int]],
    *,
    width: int = 6,
) -> None:
    """Draw one consistently centred grey AOTY-like stroke with black edge."""

    draw.line(points, fill=(0, 0, 0, 255), width=width + 4, joint="curve")
    draw.line(points, fill=(150, 154, 160, 255), width=width, joint="curve")


def render_status_emoji(key: str) -> bytes:
    """Render a transparent like, tracklist or review icon on the same grid."""

    name = str(key).casefold()
    if name not in STATUS_EMOJI_NAMES:
        raise ValueError("nieznany klucz emoji statusu")

    size = SCORE_EMOJI_SIZE
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    color = (150, 154, 160, 255)

    if name == "like":
        font = _font(64)
        text = "♥"
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font, stroke_width=3)
        draw.text(
            ((size - (right - left)) / 2 - left, (size - (bottom - top)) / 2 - top - 2),
            text,
            font=font,
            fill=color,
            stroke_width=3,
            stroke_fill=(0, 0, 0, 255),
        )
    elif name == "tracklist":
        # 1/2/3 and their three lines are deliberately centred as one group.
        font = _font(25)
        for index, y in enumerate((27, 48, 69), start=1):
            label = str(index)
            left, top, right, bottom = draw.textbbox((0, 0), label, font=font, stroke_width=2)
            draw.text(
                (20 - (right - left) / 2 - left, y - (bottom - top) / 2 - top),
                label,
                font=font,
                fill=color,
                stroke_width=2,
                stroke_fill=(0, 0, 0, 255),
            )
            _outlined_line(draw, [(36, y), (76, y)], width=5)
    else:
        # Paper with three text lines: clearer at emoji scale than a font glyph.
        draw.rounded_rectangle((25, 17, 70, 79), radius=5, fill=(0, 0, 0, 255))
        draw.rounded_rectangle((28, 20, 67, 76), radius=3, outline=color, width=4)
        for y, end in ((36, 58), (48, 61), (60, 54)):
            _outlined_line(draw, [(36, y), (end, y)], width=4)

    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    encoded = output.getvalue()
    if len(encoded) > SCORE_EMOJI_MAX_BYTES:
        raise ValueError("wygenerowane emoji statusu przekracza limit Discorda")
    return encoded


class ScoreEmojiSynchronizer:
    """Gradually create the 101 score assets without blocking bot startup."""

    def __init__(self, client: discord.Client):
        self.client = client
        self._lock = asyncio.Lock()
        self._application_id: int | None = None

    async def _application_route(self, method: str, path: str, **parameters):
        if self._application_id is None:
            application_id = getattr(self.client, "application_id", None)
            if application_id is None:
                application_id = (await self.client.application_info()).id
            self._application_id = int(application_id)
        return discord.http.Route(
            method,
            path,
            application_id=self._application_id,
            **parameters,
        )

    async def _list_application_emojis(self) -> list[dict]:
        route = await self._application_route("GET", "/applications/{application_id}/emojis")
        payload = await self.client.http.request(route)
        return list((payload or {}).get("items") or [])

    @staticmethod
    def _image_data_uri(image: bytes) -> str:
        return "data:image/png;base64," + base64.b64encode(image).decode("ascii")

    def load_cached(self) -> None:
        """Make already-uploaded emoji available immediately after restart."""

        set_score_emojis(DB.get_score_emoji_map(render_version=SCORE_EMOJI_RENDER_VERSION))

    async def sync_all(self) -> None:
        """Upload only missing score tiles, one at a time with gentle pacing."""

        async with self._lock:
            application_emojis = await self._list_application_emojis()
            by_name = {
                str(emoji.get("name")): emoji
                for emoji in application_emojis
                if emoji.get("name")
            }
            cached = DB.get_score_emoji_map(render_version=SCORE_EMOJI_RENDER_VERSION)
            set_score_emojis(cached)
            created = 0

            for score in [None, *range(101)]:
                name = score_emoji_name(score)
                stored_score = -1 if score is None else score
                existing = by_name.get(name)
                if existing is not None:
                    DB.save_score_emoji(
                        stored_score,
                        existing["id"],
                        name,
                        SCORE_EMOJI_RENDER_VERSION,
                    )
                    continue

                route = await self._application_route(
                    "POST",
                    "/applications/{application_id}/emojis",
                )
                try:
                    created_emoji = await self.client.http.request(
                        route,
                        json={
                            "name": name,
                            "image": self._image_data_uri(render_score_emoji(score)),
                        },
                    )
                except Exception as exc:
                    # Do not fail the bot if Discord temporarily rate-limits
                    # custom emoji creation. A next reconnect continues from
                    # the first missing score.
                    label = "NR" if score is None else str(score)
                    print(f"[SCORE EMOJI] {label}: {type(exc).__name__}: {exc}")
                    break

                DB.save_score_emoji(
                    stored_score,
                    created_emoji["id"],
                    name,
                    SCORE_EMOJI_RENDER_VERSION,
                )
                created += 1
                set_score_emojis(
                    DB.get_score_emoji_map(render_version=SCORE_EMOJI_RENDER_VERSION)
                )
                # Prevent an initial 101-emoji bootstrap from competing with
                # command responses or Discord's application-emoji limits.
                await asyncio.sleep(1.2)

            self.load_cached()
            print(
                "[SCORE EMOJI] Dostępne "
                f"{len(DB.get_score_emoji_map(render_version=SCORE_EMOJI_RENDER_VERSION))}/102; "
                f"utworzono teraz {created}."
            )


class StatusEmojiSynchronizer(ScoreEmojiSynchronizer):
    """Create the three shared transparent release-flag emoji after startup."""

    def load_cached(self) -> None:
        set_status_emojis(DB.get_status_emoji_map(render_version=STATUS_EMOJI_RENDER_VERSION))

    async def sync_all(self) -> None:
        async with self._lock:
            application_emojis = await self._list_application_emojis()
            by_name = {
                str(emoji.get("name")): emoji
                for emoji in application_emojis
                if emoji.get("name")
            }
            for key, name in STATUS_EMOJI_NAMES.items():
                existing = by_name.get(name)
                if existing is None:
                    route = await self._application_route(
                        "POST", "/applications/{application_id}/emojis"
                    )
                    try:
                        existing = await self.client.http.request(
                            route,
                            json={
                                "name": name,
                                "image": self._image_data_uri(render_status_emoji(key)),
                            },
                        )
                    except Exception as exc:
                        print(f"[STATUS EMOJI] {key}: {type(exc).__name__}: {exc}")
                        continue
                    await asyncio.sleep(1.2)
                DB.save_status_emoji(
                    key,
                    existing["id"],
                    name,
                    STATUS_EMOJI_RENDER_VERSION,
                )
            self.load_cached()
            print("[STATUS EMOJI] Zsynchronizowano transparentne flagi wydania.")
