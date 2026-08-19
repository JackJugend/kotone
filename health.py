"""Tiny HTTP health server for Railway.

The endpoint deliberately checks only Kotone itself (Discord readiness,
SQLite and both long-running workers). It never calls AOTY, so a third-party
outage cannot make Railway reject an otherwise healthy bot deployment.
"""

from __future__ import annotations

import asyncio
import time

from aiohttp import web

from cover_badges import render_must_hear_png
from database import DB
from http_client import HTTP
from must_hear import (
    cover_token,
    marked_cover_endpoint_enabled,
    must_hear_album,
)
from services import DATA
from settings import PORT
from source_switches import SOURCES
import lastfm
from stats_cover_cache import load_cover_bytes


class HealthServer:
    def __init__(self, client, monitor, background=None):
        self.client = client
        self.monitor = monitor
        self.background = background
        self.monitor_task = None
        self.background_task = None
        self.runner: web.AppRunner | None = None
        self.started_at = time.time()
        # Compact, non-sensitive diagnostics for generated Must Hear covers.
        # A Discord thumbnail failure otherwise disappears without a trace.
        self.must_hear_cover_requests = 0
        self.must_hear_cover_served = 0
        self.must_hear_cover_last_failure: str | None = None

    def bind_worker_tasks(self, *, monitor_task, background_task) -> None:
        """Expose the exact long-running tasks used by bot.py to readiness."""

        self.monitor_task = monitor_task
        self.background_task = background_task

    @staticmethod
    def _task_state(task) -> str:
        if task is None:
            return "not_started"
        if task.cancelled():
            return "cancelled"
        if not task.done():
            return "running"

        try:
            error = task.exception()
        except (asyncio.CancelledError, RuntimeError):
            return "cancelled"
        return "failed" if error is not None else "stopped"

    async def _health(self, request: web.Request) -> web.Response:
        database_ok = DB.health()
        discord_ready = self.client.is_ready() and not self.client.is_closed()
        monitor_state = self._task_state(self.monitor_task)
        background_state = self._task_state(self.background_task)
        monitor_ok = monitor_state == "running"
        background_ok = background_state == "running"
        ok = database_ok and discord_ready and monitor_ok and background_ok

        return web.json_response(
            {
                "ok": ok,
                "discord_ready": discord_ready,
                "database_ok": database_ok,
                "monitor_ok": monitor_ok,
                "background_ok": background_ok,
                "workers": {
                    "monitor": monitor_state,
                    "background": background_state,
                },
                "uptime_seconds": int(time.time() - self.started_at),
                "monitor_last_success": self.monitor.last_success_at,
                "background_last_success": (
                    self.background.last_success_at
                    if self.background is not None
                    else None
                ),
                "aoty_transport": HTTP.status(),
                "musicbrainz": DATA.musicbrainz_status(),
                "lastfm": lastfm.LASTFM.status(),
                "source_switches": SOURCES.status(),
                "must_hear_badges": {
                    "endpoint_enabled": marked_cover_endpoint_enabled(),
                    "requests": self.must_hear_cover_requests,
                    "served": self.must_hear_cover_served,
                    "last_failure": self.must_hear_cover_last_failure,
                },
            },
            status=200 if ok else 503,
        )

    async def _live(self, request: web.Request) -> web.Response:
        # Liveness is intentionally weaker than readiness and is useful for
        # manual diagnostics. Railway should use /health.
        database_ok = DB.health()
        return web.json_response(
            {
                "ok": database_ok,
                "database_ok": database_ok,
            },
            status=200 if database_ok else 503,
        )

    async def _must_hear_cover(self, request: web.Request) -> web.Response:
        """Serve a cached orange-tag cover only for an in-scope release."""

        self.must_hear_cover_requests += 1
        album_id = str(request.match_info.get("album_id") or "").strip()
        token = str(request.match_info.get("token") or "").strip()
        details = await asyncio.to_thread(DB.get_release_details, album_id)
        if details is None:
            # Older/imported rows may have the original rating card (and its
            # cover) but not yet a matching ``releases`` cache row.
            details = await asyncio.to_thread(DB.get_any_active_rating_for_album, album_id)
        if not details:
            self.must_hear_cover_last_failure = "release_not_cached"
            raise web.HTTPNotFound()
        cover_url = str(details.get("cover") or "").strip()

        def original_cover(reason: str) -> None:
            """Keep Discord thumbnails visible if the generated badge fails.

            A 302 is deliberately preferable to a 404 here: Discord follows
            image redirects, while a failed generated thumbnail otherwise
            leaves the entire card with no cover at all.
            """

            self.must_hear_cover_last_failure = reason
            if cover_url.startswith(("https://", "http://")):
                raise web.HTTPFound(location=cover_url)
            raise web.HTTPNotFound()

        if token != cover_token(album_id, cover_url):
            original_cover("cover_token_mismatch")
        if not must_hear_album(
            details.get("user_score"),
            details.get("ratings_count"),
            details.get("critic_score"),
            details.get("critic_reviews_count"),
            album_id=album_id,
            official=details.get("must_hear"),
        ):
            original_cover("no_longer_eligible")
        content = await asyncio.to_thread(load_cover_bytes, cover_url)
        if not content:
            original_cover("cover_unavailable")
        try:
            marked = await asyncio.to_thread(render_must_hear_png, content)
        except Exception:
            original_cover("cover_render_failed")
        self.must_hear_cover_served += 1
        self.must_hear_cover_last_failure = None
        return web.Response(
            body=marked,
            content_type="image/png",
            headers={"Cache-Control": "public, max-age=86400, immutable"},
        )

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/health", self._health)
        app.router.add_get("/live", self._live)
        app.router.add_get(
            "/must-hear-cover/{album_id}/{token}.png",
            self._must_hear_cover,
        )

        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "0.0.0.0", PORT)
        await site.start()
        print(f"[HEALTH] HTTP :{PORT} /health")

    async def stop(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None
