"""Background monitor for configured AOTY users.

The monitor is intentionally thin: network/cache policy lives in services.py,
state lives in database.py, and Discord rendering lives here/views.py.

Reliability rules:
- only users from config.json are ever persisted/monitored;
- quick syncs run often, full syncs run less often;
- failed Discord sends do not advance the saved score;
- profile metadata is refreshed independently from ratings;
- low-priority enrichment gradually fills SQLite with review/track/release data;
- all AOTY requests use the global priority/rate-limit transport.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict

import discord
import requests

import aoty
from database import DB
from http_client import PRIORITY_BACKGROUND, PRIORITY_INTERACTIVE
from services import DATA
from settings import (
    CHANNEL_ID,
    CHECK_INTERVAL,
    DETAIL_ENRICH_PER_CYCLE,
    FULL_SYNC_INTERVAL,
    PROFILE_SYNC_INTERVAL,
    PROFILE_RATING_ARCHIVE_FORMATS_PER_CYCLE,
    RELEASE_ENRICH_PER_CYCLE,
    USER_CHANNELS,
    USERS,
)
from shared import load_release_variables, rating_flags_text, score_color, score_icon
from views import SingleRatingView

MONITOR_STATE_VERSION = 3


class RatingMonitor:
    """Poll configured accounts and announce new/changed scores."""

    def __init__(self, client: discord.Client):
        self.client = client
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._stop_event = asyncio.Event()
        self.last_cycle_at: float | None = None
        self.last_success_at: float | None = None
        self.last_error: str | None = None

    def stop(self) -> None:
        self._stop_event.set()

    async def _sleep(self, seconds: float) -> None:
        """Sleep but wake immediately during shutdown."""
        try:
            await asyncio.wait_for(
                self._stop_event.wait(),
                timeout=max(0.0, seconds),
            )
        except asyncio.TimeoutError:
            pass

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

    async def send_new_rating(
        self,
        username: str,
        item: dict,
        avatar: str | None = None,
    ) -> bool:
        channel = await self.get_discord_channel(username)
        if channel is None:
            return False

        variables = await load_release_variables(item, username=username)
        flags = rating_flags_text(item)
        flags_text = f"  •  {flags}" if flags else ""

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

        footer_text = f"{username} AOTY  •  {variables.date}{flags_text}  ⚠️"
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
            message = await channel.send(embed=embed, view=view)
            view.bind_message(message)
            print(
                f"[DISCORD] Nowa ocena: {variables.artist} — "
                f"{variables.album} ({variables.score}/100)"
            )
            return True
        except Exception as exc:
            print(f"[DISCORD] Błąd wysyłania: {type(exc).__name__}: {exc}")
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

        variables = await load_release_variables(item, username=username)
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

        footer_text = f"{username} AOTY  •  {variables.date}{flags_text}  🔄"
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
            message = await channel.send(embed=embed, view=view)
            view.bind_message(message)
            print(
                f"[DISCORD] Zmiana: {variables.artist} — {variables.album} "
                f"{old_score} → {variables.score}"
            )
            return True
        except Exception as exc:
            print(f"[DISCORD] Błąd wysyłania: {type(exc).__name__}: {exc}")
            return False

    async def _refresh_profile_if_due(self, username: str, *, manual: bool) -> None:
        stamps = DB.sync_timestamps(username)
        age = time.time() - float(stamps.get("profile_synced_at") or 0)
        if age < PROFILE_SYNC_INTERVAL and not manual:
            return

        try:
            await DATA.sync_profile(
                username,
                priority=PRIORITY_INTERACTIVE if manual else PRIORITY_BACKGROUND,
            )
            print(f"[PROFILE] {username}: profil zapisany w SQLite.")
        except Exception as exc:
            print(f"[PROFILE] {username}: {type(exc).__name__}: {exc}")

    async def check_user(
        self,
        username: str,
        *,
        manual: bool = False,
        allow_full: bool = True,
    ) -> dict:
        """Check one configured account. Manual checks never write other users."""

        canonical = DB.canonical_username(username)
        if canonical is None:
            return {"error": "użytkownik nie jest wpisany w config.json"}
        username = canonical

        lock = self._locks[username.casefold()]
        if lock.locked():
            return {"busy": True}

        async with lock:
            prefix = "MANUAL" if manual else "AOTY"
            print(f"[{prefix}] Sprawdzam {username}...")

            await self._refresh_profile_if_due(username, manual=manual)

            stamps = DB.sync_timestamps(username)
            full_due = (
                not stamps.get("full_ratings_synced_at")
                or time.time() - float(stamps.get("full_ratings_synced_at") or 0)
                >= FULL_SYNC_INTERVAL
            )
            # Expensive full scans are staggered: the background loop allows
            # only one configured user to perform a full scan per cycle. A
            # manual /check may still perform it immediately when due.
            full = bool(full_due and (manual or allow_full))

            try:
                ratings = await DATA.fetch_ratings_live(
                    username,
                    full=full,
                    priority=PRIORITY_INTERACTIVE if manual else PRIORITY_BACKGROUND,
                )
            except aoty.AOTYRateLimit as exc:
                message = str(exc)
                DB.mark_sync_error(username, message)
                print(f"[AOTY] {username}: {message}")
                return {"error": message}
            except requests.RequestException as exc:
                message = f"błąd HTTP: {exc}"
                DB.mark_sync_error(username, message)
                print(f"[AOTY] {username}: {message}")
                return {"error": message}
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                DB.mark_sync_error(username, message)
                print(f"[AOTY] {username}: {message}")
                return {"error": message}

            if not ratings:
                # An empty enabled-format snapshot can be perfectly valid
                # (e.g. the user only rates formats disabled for notifications).
                # Never treat it as a destructive signal: no cached rows are
                # removed here. The silent profile archive can still fill the
                # disabled formats below.
                DB.set_monitor_version(username, MONITOR_STATE_VERSION)
                DB.mark_sync_success(username, full=full)
                await DATA.archive_profile_ratings(
                    username,
                    formats_per_cycle=PROFILE_RATING_ARCHIVE_FORMATS_PER_CYCLE,
                )
                await DATA.enrich_user(
                    username,
                    detail_limit=DETAIL_ENRICH_PER_CYCLE,
                    release_limit=RELEASE_ENRICH_PER_CYCLE,
                )
                DB.backup_if_due()
                print(
                    f"[AOTY] {username}: 0 ocen w formatach monitorowanych; "
                    "cache profilu pozostaje bez zmian."
                )
                return {
                    "ratings": 0,
                    "new": 0,
                    "changed": 0,
                    "full": full,
                }

            existing = DB.get_ratings_map(username, include_inactive=True)
            first_sync = not stamps.get("ratings_synced_at")
            monitor_version = DB.get_monitor_version(username)

            # First deploy / coverage migration: seed silently. This avoids a
            # flood of historical notifications after a schema/config upgrade.
            if first_sync or monitor_version != MONITOR_STATE_VERSION:
                DB.upsert_ratings(
                    username,
                    ratings,
                    record_history=False,
                    mark_missing_inactive=False,
                )
                # Do not globally deactivate unseen rows here. Full monitor
                # sync covers only notification-enabled formats; the silent
                # per-format archive owns format-specific removal handling.
                DB.set_monitor_version(username, MONITOR_STATE_VERSION)
                DB.mark_sync_success(username, full=full)
                await DATA.archive_profile_ratings(
                    username,
                    formats_per_cycle=PROFILE_RATING_ARCHIVE_FORMATS_PER_CYCLE,
                )
                await DATA.enrich_user(
                    username,
                    detail_limit=DETAIL_ENRICH_PER_CYCLE,
                    release_limit=RELEASE_ENRICH_PER_CYCLE,
                )
                DB.backup_if_due()
                print(
                    f"[AOTY] {username}: seed/migracja — zapisano "
                    f"{len(ratings)} ocen bez powiadomień."
                )
                return {
                    "seeded": True,
                    "ratings": len(ratings),
                    "new": 0,
                    "changed": 0,
                    "full": full,
                }

            new_items: list[dict] = []
            changed_items: list[tuple[dict, str]] = []
            unchanged_items: list[dict] = []

            for item in ratings:
                album_id = str(item["album_id"])
                previous = existing.get(album_id)
                if previous is None or not previous.get("active", True):
                    new_items.append(item)
                    continue

                old_score = str(previous.get("score") or "")
                new_score = str(item.get("score") or "")
                if old_score != new_score:
                    changed_items.append((item, old_score))
                else:
                    unchanged_items.append(item)

            print(
                f"[AOTY] {username}: ratings={len(ratings)}, "
                f"new={len(new_items)}, changed={len(changed_items)}, full={full}"
            )

            avatar = DB.get_avatar(username)
            sent_new = 0
            sent_changed = 0

            # Metadata for unchanged rows can be updated immediately.
            DB.upsert_ratings(
                username,
                unchanged_items,
                record_history=False,
            )

            for item in reversed(new_items):
                if self._stop_event.is_set():
                    break
                sent = await self.send_new_rating(username, item, avatar)
                if not sent:
                    continue
                DB.upsert_rating(username, item, record_history=True)
                sent_new += 1
                await self._sleep(0.5)

            for item, old_score in reversed(changed_items):
                if self._stop_event.is_set():
                    break
                sent = await self.send_changed_rating(
                    username,
                    item,
                    old_score,
                    avatar,
                )
                if not sent:
                    continue
                DB.upsert_rating(username, item, record_history=True)
                sent_changed += 1
                await self._sleep(0.5)

            # Missing-rating deactivation is intentionally handled by the
            # per-format archive. A parser hiccup in one AOTY route must never
            # make unrelated cached profile data disappear.

            DB.set_monitor_version(username, MONITOR_STATE_VERSION)
            DB.mark_sync_success(username, full=full)

            # Fill durable cache slowly, *after* notification work. If AOTY is
            # unhappy, enrichment stops without harming the monitor result.
            await DATA.archive_profile_ratings(
                username,
                formats_per_cycle=PROFILE_RATING_ARCHIVE_FORMATS_PER_CYCLE,
            )
            await DATA.enrich_user(
                username,
                detail_limit=DETAIL_ENRICH_PER_CYCLE,
                release_limit=RELEASE_ENRICH_PER_CYCLE,
            )
            DB.backup_if_due()

            return {
                "ratings": len(ratings),
                "new": len(new_items),
                "changed": len(changed_items),
                "sent_new": sent_new,
                "sent_changed": sent_changed,
                "full": full,
            }

    async def run(self) -> None:
        await self.client.wait_until_ready()

        summary = DB.summary()
        print()
        print("==============================")
        print("        KOTONE")
        print("==============================")
        print("Monitoruję:", ", ".join(USERS) if USERS else "—")
        print("Interwał:", CHECK_INTERVAL, "sekund")
        print("SQLite:", summary["path"])
        print("DB users/ratings:", summary["users"], "/", summary["ratings"])
        print("==============================")
        print()

        while not self.client.is_closed() and not self._stop_event.is_set():
            self.last_cycle_at = time.time()
            cycle_ok = True

            full_scan_used = False

            for username in USERS:
                if self._stop_event.is_set():
                    break

                try:
                    result = await self.check_user(
                        username,
                        allow_full=not full_scan_used,
                    )
                except Exception as exc:
                    # A parser/Discord regression for one user must never kill
                    # the long-running monitor task. The next cycle retries.
                    cycle_ok = False
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    DB.mark_sync_error(username, self.last_error)
                    print(
                        f"[MONITOR] Nieobsłużony błąd {username}: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    await self._sleep(1.0)
                    continue

                if result.get("full"):
                    full_scan_used = True

                if result.get("error"):
                    cycle_ok = False
                    self.last_error = str(result["error"])
                await self._sleep(1.0)

            if cycle_ok:
                self.last_success_at = time.time()
                self.last_error = None

            print(f"[BOT] Następne sprawdzenie za {CHECK_INTERVAL} sekund.")
            await self._sleep(CHECK_INTERVAL)
