"""Automatic and manual AOTY rating update monitor.

All polling/state/sending logic lives here.  ``bot.py`` only starts the
monitor and registers commands, while ``commands/check.py`` calls the same
``check_user`` method manually.  This keeps automatic and manual checks
identical and prevents two checks for the same AOTY account from racing.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict

import discord
import requests

import aoty
from settings import (
    CHANNEL_ID,
    CHECK_INTERVAL,
    USER_CHANNELS,
    USERS,
)
from shared import (
    build_release_variables,
    rating_flags_text,
    score_color,
    score_icon,
)
from state import STORE
from views import SingleRatingView

# Version of the *monitor coverage*, not data.json itself.  Older Kotone
# versions remembered mainly the default album route.  When this version is
# first deployed we silently seed every enabled format once, otherwise old
# singles/EPs/etc. could be announced as brand-new ratings.
MONITOR_STATE_VERSION = 2


def _remembered_rating(item: dict) -> dict:
    """Small, backwards-compatible state record for one user rating."""
    return {
        "score": str(item.get("score") or ""),
        "date": item.get("date"),
        "artist": item.get("artist"),
        "album": item.get("album"),
        "release_format": item.get("release_format"),
        "has_review": bool(item.get("has_review")),
        "has_track_ratings": bool(item.get("has_track_ratings")),
        "liked": bool(item.get("liked")),
        "review_url": item.get("review_url"),
    }


def _merge_metadata(target: dict, item: dict) -> None:
    """Update non-score metadata without changing notification semantics."""
    target["date"] = item.get("date")
    target["artist"] = item.get("artist")
    target["album"] = item.get("album")

    if item.get("release_format"):
        target["release_format"] = item.get("release_format")

    target["has_review"] = bool(item.get("has_review"))
    target["has_track_ratings"] = bool(item.get("has_track_ratings"))
    target["liked"] = bool(item.get("liked"))

    if item.get("review_url"):
        target["review_url"] = item.get("review_url")


class RatingMonitor:
    """Poll AOTY accounts and announce new/changed ratings."""

    def __init__(self, client: discord.Client):
        self.client = client
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def get_discord_channel(self, username: str):
        channel_id = USER_CHANNELS.get(username.casefold(), CHANNEL_ID)
        channel = self.client.get_channel(channel_id)

        if channel is not None:
            return channel

        try:
            return await self.client.fetch_channel(channel_id)
        except Exception as exc:
            print(
                f"[DISCORD] Nie znaleziono kanału dla {username} "
                f"({channel_id}): {type(exc).__name__}: {exc}"
            )
            return None

    async def _album_details(self, item: dict) -> dict:
        url = item.get("url")
        if not url:
            return {}

        try:
            return await asyncio.to_thread(aoty.get_album_details, url)
        except aoty.AOTYRateLimit:
            # Szczegóły są dodatkiem. Sama ocena nadal może zostać wysłana.
            return {}
        except Exception as exc:
            print(
                f"[AOTY] Szczegóły {item.get('artist')} — "
                f"{item.get('album')}: {type(exc).__name__}: {exc}"
            )
            return {}

    async def send_new_rating(
        self,
        username: str,
        item: dict,
        avatar: str | None = None,
    ) -> bool:
        channel = await self.get_discord_channel(username)
        if channel is None:
            return False

        details = await self._album_details(item)
        variables = build_release_variables(item, details)
        flags = rating_flags_text(item)
        flags_text = f"  •  {flags}" if flags else ""

        # Appearance intentionally kept from the user's current monitor.
        embed = discord.Embed(
            title=variables.display_album,
            url=variables.url,
            description=f"**{variables.display_artist}**",
            color=score_color(variables.score),
        )
        embed.add_field(
            name=f"**{variables.score}**  {score_icon(variables.score)}",
            value=" ",
            inline=True,
        )

        if variables.cover:
            embed.set_thumbnail(url=variables.cover)

        footer_text = (
            f"{username} AOTY  •  {variables.date}{flags_text}  ⚠️"
        )
        if avatar:
            embed.set_footer(text=footer_text, icon_url=avatar)
        else:
            embed.set_footer(text=footer_text)

        view = SingleRatingView(
            username=username,
            item=item,
            main_embed=embed,
        )

        try:
            await channel.send(embed=embed, view=view)
            print(
                f"[DISCORD] Wysłano: {variables.artist} — "
                f"{variables.album} ({variables.score}/100)"
            )
            return True
        except Exception as exc:
            print(
                f"[DISCORD] Błąd wysyłania: "
                f"{type(exc).__name__}: {exc}"
            )
            return False

    async def send_changed_rating(
        self,
        username: str,
        item: dict,
        old_score: str,
        avatar: str | None = None,
    ) -> bool:
        channel = await self.get_discord_channel(username)
        if channel is None:
            return False

        details = await self._album_details(item)
        variables = build_release_variables(item, details)
        flags = rating_flags_text(item)
        flags_text = f"  •  {flags}" if flags else ""

        embed = discord.Embed(
            title=variables.display_album,
            url=variables.url,
            description=variables.display_artist,
            color=score_color(variables.score),
        )
        embed.add_field(
            name=(
                f"*{old_score}*  ➞  **{variables.score}**  "
                f"{score_icon(variables.score)}"
            ),
            value=" ",
            inline=True,
        )

        if variables.cover:
            embed.set_thumbnail(url=variables.cover)

        footer_text = (
            f"{username} AOTY  •  {variables.date}{flags_text}  🔄"
        )
        if avatar:
            embed.set_footer(text=footer_text, icon_url=avatar)
        else:
            embed.set_footer(text=footer_text)

        view = SingleRatingView(
            username=username,
            item=item,
            main_embed=embed,
        )

        try:
            await channel.send(embed=embed, view=view)
            print(
                f"[DISCORD] Wysłano zmianę: {variables.artist} — "
                f"{variables.album} {old_score} → {variables.score}"
            )
            return True
        except Exception as exc:
            print(
                f"[DISCORD] Błąd wysyłania: "
                f"{type(exc).__name__}: {exc}"
            )
            return False

    async def check_user(self, username: str, *, manual: bool = False) -> dict:
        """Run one complete update check and return a compact result dict."""
        username = str(username).strip()
        lock = self._locks[username.casefold()]

        # Manual command should respond instead of waiting behind the monitor.
        if lock.locked():
            return {"busy": True}

        async with lock:
            prefix = "MANUAL" if manual else "AOTY"
            print(f"[{prefix}] Sprawdzam {username}...")

            try:
                ratings = await asyncio.to_thread(aoty.get_ratings, username)
            except aoty.AOTYRateLimit as exc:
                message = str(exc)
                print(f"[AOTY] {username}: {message}")
                return {"error": message}
            except requests.RequestException as exc:
                message = f"błąd HTTP: {exc}"
                print(f"[AOTY] {username}: {message}")
                return {"error": message}
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                print(f"[AOTY] {username}: {message}")
                return {"error": message}

            if not ratings:
                message = "nie znaleziono ocen w włączonych formatach"
                print(f"[AOTY] {username}: {message}.")
                return {"error": message, "ratings": 0}

            print(f"[AOTY] {username}: znaleziono {len(ratings)} ocen.")

            users_data = STORE.data.setdefault("users", {})
            user_data = users_data.get(username)

            # First ever run for this account: remember everything, send nothing.
            if user_data is None:
                user_data = {
                    "ratings": {},
                    "format_monitor_version": MONITOR_STATE_VERSION,
                }
                users_data[username] = user_data
                known = user_data["ratings"]

                for item in ratings:
                    known[str(item["album_id"])] = _remembered_rating(item)

                STORE.save()
                print(
                    f"[AOTY] {username}: pierwsze uruchomienie — "
                    "zapamiętuję aktualny stan."
                )
                return {
                    "seeded": True,
                    "ratings": len(ratings),
                    "new": 0,
                    "changed": 0,
                }

            known = user_data.setdefault("ratings", {})

            # One-time migration from the old album-only monitor.  Merge the
            # current enabled-format state without flooding Discord with old
            # singles/EPs/etc.  Existing remembered ratings are preserved.
            if user_data.get("format_monitor_version") != MONITOR_STATE_VERSION:
                for item in ratings:
                    album_id = str(item["album_id"])
                    current = known.get(album_id)
                    if current is None:
                        known[album_id] = _remembered_rating(item)
                    else:
                        # Keep the old score so no legitimate previous state is
                        # silently rewritten during migration; enrich metadata.
                        _merge_metadata(current, item)

                user_data["format_monitor_version"] = MONITOR_STATE_VERSION
                STORE.save()
                print(
                    f"[AOTY] {username}: migracja monitora formatów — "
                    "zapamiętuję aktualny stan bez starych powiadomień."
                )
                return {
                    "seeded": True,
                    "migrated": True,
                    "ratings": len(ratings),
                    "new": 0,
                    "changed": 0,
                }

            new_items: list[dict] = []
            changed_items: list[tuple[dict, str]] = []

            for item in ratings:
                album_id = str(item["album_id"])
                previous = known.get(album_id)

                if previous is None:
                    new_items.append(item)
                    continue

                old_score = str(previous.get("score") or "")
                new_score = str(item.get("score") or "")

                if old_score != new_score:
                    changed_items.append((item, old_score))

            print(
                f"[AOTY] {username}: nowych={len(new_items)}, "
                f"zmienionych={len(changed_items)}"
            )

            try:
                avatar = await asyncio.to_thread(aoty.get_user_avatar, username)
            except Exception as exc:
                print(
                    f"[AOTY] {username}: nie udało się pobrać avatara: "
                    f"{type(exc).__name__}: {exc}"
                )
                avatar = None

            sent_new = 0
            sent_changed = 0

            # AOTY lists newest first. Reversing only the changed subset makes
            # several notifications arrive chronologically oldest -> newest.
            for item in reversed(new_items):
                sent = await self.send_new_rating(username, item, avatar)
                if not sent:
                    continue

                known[str(item["album_id"])] = _remembered_rating(item)
                STORE.save()
                sent_new += 1
                await asyncio.sleep(1)

            for item, old_score in reversed(changed_items):
                sent = await self.send_changed_rating(
                    username,
                    item,
                    old_score,
                    avatar,
                )
                if not sent:
                    continue

                known[str(item["album_id"])] = _remembered_rating(item)
                STORE.save()
                sent_changed += 1
                await asyncio.sleep(1)

            # Refresh metadata only where the remembered score equals the live
            # score. A failed Discord send must never make a score change vanish.
            for item in ratings:
                album_id = str(item["album_id"])
                current = known.get(album_id)
                if current is None:
                    continue

                if str(current.get("score") or "") == str(item.get("score") or ""):
                    _merge_metadata(current, item)

            STORE.save()

            return {
                "ratings": len(ratings),
                "new": len(new_items),
                "changed": len(changed_items),
                "sent_new": sent_new,
                "sent_changed": sent_changed,
            }

    async def run(self) -> None:
        """Background polling loop."""
        await self.client.wait_until_ready()

        print()
        print("==============================")
        print("        KOTONE")
        print("==============================")
        print("Monitoruję:", ", ".join(USERS) if USERS else "—")
        print("Interwał:", CHECK_INTERVAL, "sekund")
        print("STATUS:", self.client.status)
        print("ACTIVITY:", self.client.activity)
        print("==============================")
        print()

        while not self.client.is_closed():
            for username in USERS:
                await self.check_user(username)
                await asyncio.sleep(2)

            print(
                f"[BOT] Następne sprawdzenie za {CHECK_INTERVAL} sekund."
            )
            await asyncio.sleep(CHECK_INTERVAL)
