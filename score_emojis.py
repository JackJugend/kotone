"""Deterministiczne emoji ocen 0–100 w stylu AOTY.

Każdy kafelek ma czarną podstawę, białą liczbę i pasek w aktualnej palecie
Kotone. Obraz powstaje lokalnie, więc AI ani zewnętrzna usługa nie mogą pomylić
cyfry oceny. Emoji są Application Emojis: należą do Kotone, nie do guildy.
"""

from __future__ import annotations

import asyncio
import base64
from io import BytesIO
from pathlib import Path
import time

import discord
from PIL import Image, ImageDraw, ImageFont

from database import DB
from score_emoji_registry import set_score_emojis
from status_emoji_registry import set_status_emojis

SCORE_EMOJI_PREFIX = "score_"
SCORE_EMOJI_RENDER_VERSION = "aoty-tile-v8-large-centred"
SCORE_EMOJI_SIZE = 128
SCORE_EMOJI_MAX_BYTES = 256 * 1024
STATUS_EMOJI_RENDER_VERSION = "aoty-flags-v4-large-centred"
STATUS_EMOJI_NAMES = {
    "like": "like",
    "tracklist": "tracklist",
    "review": "review",
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
    """Use the bundled Noto Sans so the render is identical on Railway."""

    for path in (
        Path(__file__).with_name("assets") / "NotoSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
    ):
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _regular_font(size: int) -> ImageFont.FreeTypeFont:
    """AOTY-like light score numerals, bundled with the bot."""

    for path in (
        Path(__file__).with_name("assets") / "NotoSans-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/arial.ttf",
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
    # AOTY's number and progress bar float directly over Discord's background;
    # the PNG itself remains transparent around and between both elements.
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    # The rating is the primary visual element. Use almost the full width,
    # then centre the real glyph bounds rather than its font baseline.
    font = _regular_font(90 if value is None or value < 100 else 71)
    text = "NR" if value is None else str(value)
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    width = right - left
    height = bottom - top
    # A geometric centre is not enough for narrow glyphs such as ``1``: the
    # visible ink then looks left-heavy next to a centred progress bar.
    # Compensate only that optical imbalance, scaled for the 96 px source.
    narrow_digit_offset = text.count("1") * 4
    draw.text(
        ((size - width) / 2 - left + narrow_digit_offset, 4 + (91 - height) / 2 - top),
        text,
        font=font,
        fill=(230, 232, 235, 255),
    )

    if value is None:
        output = BytesIO()
        image.save(output, format="PNG", optimize=True)
        return output.getvalue()

    bar_left, bar_top, bar_right, bar_bottom = 9, 104, size - 9, 120
    # AOTY keeps the unfilled progress track visible in a light cool grey.
    draw.rectangle((bar_left, bar_top, bar_right, bar_bottom), fill=(119, 124, 132, 255))
    filled_right = bar_left + round((bar_right - bar_left) * value / 100)
    if value > 0:
        draw.rectangle((bar_left, bar_top, max(bar_left, filled_right), bar_bottom), fill=(*_bar_color(value), 255))

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
    width: int = 7,
) -> None:
    """Draw one consistently centred grey AOTY-like stroke with black edge."""

    draw.line(points, fill=(0, 0, 0, 255), width=width + 2, joint="curve")
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
        font = _font(96)
        text = "♥"
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font, stroke_width=1)
        draw.text(
            ((size - (right - left)) / 2 - left, (size - (bottom - top)) / 2 - top - 3),
            text,
            font=font,
            fill=color,
            stroke_width=0,
        )
    elif name == "tracklist":
        # AOTY's icon uses two compact numbered rows, optically centred.
        font = _font(39)
        for index, y in enumerate((47, 83), start=1):
            label = str(index)
            left, top, right, bottom = draw.textbbox((0, 0), label, font=font, stroke_width=2)
            draw.text(
                (30 - (right - left) / 2 - left, y - (bottom - top) / 2 - top),
                label,
                font=font,
                fill=color,
                stroke_width=1,
                stroke_fill=(0, 0, 0, 255),
            )
            _outlined_line(draw, [(53, y), (104, y)], width=8)
    else:
        # A clean paper outline with a folded top-right corner.
        _outlined_line(draw, [(33, 19), (82, 19), (96, 33), (96, 108), (33, 108), (33, 19)], width=8)
        _outlined_line(draw, [(82, 19), (82, 34), (96, 34)], width=8)
        _outlined_line(draw, [(49, 65), (80, 65)], width=6)
        _outlined_line(draw, [(49, 84), (80, 84)], width=6)

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

    async def _delete_legacy_emojis(
        self,
        emojis: list[dict],
        *,
        names_or_prefixes: tuple[str, ...],
    ) -> None:
        """Remove only the old Kotone application assets after replacement."""

        for emoji in emojis:
            name = str(emoji.get("name") or "")
            if not any(name.startswith(prefix) for prefix in names_or_prefixes):
                continue
            try:
                route = await self._application_route(
                    "DELETE",
                    "/applications/{application_id}/emojis/{emoji_id}",
                    emoji_id=emoji["id"],
                )
                await self.client.http.request(route)
            except Exception as exc:
                print(f"[EMOJI] Nie usunięto starego :{name}: {type(exc).__name__}: {exc}")

    @staticmethod
    def _image_data_uri(image: bytes) -> str:
        return "data:image/png;base64," + base64.b64encode(image).decode("ascii")

    async def _create_or_replace_emoji(
        self,
        *,
        existing: dict | None,
        name: str,
        image: bytes,
    ) -> dict:
        """Upload a new image while preserving the public emoji name."""

        create_route = await self._application_route(
            "POST", "/applications/{application_id}/emojis"
        )
        if existing is None:
            return await self.client.http.request(
                create_route,
                json={"name": name, "image": self._image_data_uri(image)},
            )

        # A previous deploy can have been interrupted after creating a
        # temporary emoji. A timestamp prevents that stale object from ever
        # blocking the replacement currently in progress.
        temporary_name = f"tmp_{name}_{time.time_ns() % 1_000_000_000}"[:32]
        created = await self.client.http.request(
            create_route,
            json={"name": temporary_name, "image": self._image_data_uri(image)},
        )
        delete_route = await self._application_route(
            "DELETE",
            "/applications/{application_id}/emojis/{emoji_id}",
            emoji_id=existing["id"],
        )
        await self.client.http.request(delete_route)
        rename_route = await self._application_route(
            "PATCH",
            "/applications/{application_id}/emojis/{emoji_id}",
            emoji_id=created["id"],
        )
        return await self.client.http.request(rename_route, json={"name": name})

    @staticmethod
    def _retry_delay(error: Exception) -> float | None:
        """Return a safe retry delay only for transient Discord API errors."""

        status = int(getattr(error, "status", 0) or 0)
        retry_after = float(getattr(error, "retry_after", 0) or 0)
        if status == 429:
            return max(5.0, min(900.0, retry_after or 60.0))
        if 500 <= status < 600:
            return 60.0
        return None

    async def _create_or_replace_with_retry(self, **kwargs) -> dict:
        """Wait through Discord's emoji rate limits instead of stopping sync."""

        while True:
            try:
                return await self._create_or_replace_emoji(**kwargs)
            except Exception as exc:
                delay = self._retry_delay(exc)
                if delay is None:
                    raise
                name = str(kwargs.get("name") or "emoji")
                print(
                    f"[EMOJI] :{name}: limit Discorda; "
                    f"ponawiam za {int(delay + 0.999)} s."
                )
                await asyncio.sleep(delay)

    def load_cached(self) -> None:
        """Make already-uploaded emoji available immediately after restart."""

        set_score_emojis(DB.get_score_emoji_map(render_version=SCORE_EMOJI_RENDER_VERSION))

    async def sync_all(self) -> None:
        """Upload only missing score tiles, one at a time with gentle pacing."""

        async with self._lock:
            application_emojis = await self._list_application_emojis()
            # Clean interrupted replacements *before* allocating a new
            # temporary name. Waiting for a full 102-score sync caused a
            # stale ``tmp_score_077`` to block every later score.
            await self._delete_legacy_emojis(
                application_emojis,
                names_or_prefixes=("tmp_score_",),
            )
            application_emojis = await self._list_application_emojis()
            by_name = {
                str(emoji.get("name")): emoji
                for emoji in application_emojis
                if emoji.get("name")
            }
            cached = DB.get_score_emoji_map(render_version=SCORE_EMOJI_RENDER_VERSION)
            states = DB.get_score_emoji_states()
            set_score_emojis(cached)
            created = 0

            used_scores = DB.used_rating_scores()
            ordered_scores = [None, *used_scores]
            ordered_scores.extend(score for score in range(101) if score not in used_scores)
            for score in ordered_scores:
                name = score_emoji_name(score)
                stored_score = -1 if score is None else score
                existing = by_name.get(name)
                state = states.get(stored_score) or {}
                needs_rebuild = (
                    existing is not None
                    and str(state.get("emoji_id") or "") == str(existing.get("id") or "")
                    and state.get("render_version") != SCORE_EMOJI_RENDER_VERSION
                )
                if existing is not None and not needs_rebuild:
                    DB.save_score_emoji(
                        stored_score,
                        existing["id"],
                        name,
                        SCORE_EMOJI_RENDER_VERSION,
                    )
                    continue

                try:
                    created_emoji = await self._create_or_replace_with_retry(
                        existing=existing if needs_rebuild else None,
                        name=name,
                        image=render_score_emoji(score),
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
                await asyncio.sleep(0.35)

            self.load_cached()
            if len(DB.get_score_emoji_map(render_version=SCORE_EMOJI_RENDER_VERSION)) == 102:
                # v1/v2 assets have a different visual and are no longer
                # referenced by SQLite after the new set is complete.
                await self._delete_legacy_emojis(
                    await self._list_application_emojis(),
                    names_or_prefixes=("kotone_score_", "tmp_score_"),
                )
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
            await self._delete_legacy_emojis(
                application_emojis,
                names_or_prefixes=("tmp_like", "tmp_tracklist", "tmp_review"),
            )
            application_emojis = await self._list_application_emojis()
            by_name = {
                str(emoji.get("name")): emoji
                for emoji in application_emojis
                if emoji.get("name")
            }
            states = DB.get_status_emoji_states()
            complete = True
            for key, name in STATUS_EMOJI_NAMES.items():
                existing = by_name.get(name)
                state = states.get(key) or {}
                needs_rebuild = (
                    existing is not None
                    and str(state.get("emoji_id") or "") == str(existing.get("id") or "")
                    and state.get("render_version") != STATUS_EMOJI_RENDER_VERSION
                )
                if existing is None or needs_rebuild:
                    try:
                        existing = await self._create_or_replace_with_retry(
                            existing=existing if needs_rebuild else None,
                            name=name,
                            image=render_status_emoji(key),
                        )
                    except Exception as exc:
                        print(f"[STATUS EMOJI] {key}: {type(exc).__name__}: {exc}")
                        complete = False
                        continue
                    await asyncio.sleep(0.35)
                DB.save_status_emoji(
                    key,
                    existing["id"],
                    name,
                    STATUS_EMOJI_RENDER_VERSION,
                )
            self.load_cached()
            if complete and len(DB.get_status_emoji_map(render_version=STATUS_EMOJI_RENDER_VERSION)) == 3:
                await self._delete_legacy_emojis(
                    await self._list_application_emojis(),
                    names_or_prefixes=(
                        "kotone_like",
                        "kotone_tracklist",
                        "kotone_review",
                        "tmp_like",
                        "tmp_tracklist",
                        "tmp_review",
                    ),
                )
            print("[STATUS EMOJI] Zsynchronizowano transparentne flagi wydania.")
