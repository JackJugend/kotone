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
                "must_hear_badges": {
                    "endpoint_enabled": marked_cover_endpoint_enabled(),
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

        album_id = str(request.match_info.get("album_id") or "").strip()
        token = str(request.match_info.get("token") or "").strip()
        details = await asyncio.to_thread(DB.get_release_details, album_id)
        if not details:
            raise web.HTTPNotFound()
        cover_url = str(details.get("cover") or "").strip()
        if token != cover_token(album_id, cover_url):
            raise web.HTTPNotFound()
        if not must_hear_album(
            details.get("user_score"),
            details.get("ratings_count"),
            details.get("critic_score"),
            details.get("critic_reviews_count"),
        ):
            raise web.HTTPNotFound()
        content = await asyncio.to_thread(load_cover_bytes, cover_url)
        if not content:
            raise web.HTTPNotFound()
        try:
            marked = await asyncio.to_thread(render_must_hear_png, content)
        except Exception:
            raise web.HTTPNotFound() from None
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
