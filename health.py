"""Tiny HTTP health server for Railway.

The endpoint deliberately checks only Kotone itself (Discord readiness +
SQLite).  It never calls AOTY, so a third-party outage cannot make Railway
reject an otherwise healthy bot deployment.
"""

from __future__ import annotations

import time

from aiohttp import web

from database import DB
from http_client import HTTP
from settings import PORT


class HealthServer:
    def __init__(self, client, monitor):
        self.client = client
        self.monitor = monitor
        self.runner: web.AppRunner | None = None
        self.started_at = time.time()

    async def _health(self, request: web.Request) -> web.Response:
        database_ok = DB.health()
        discord_ready = self.client.is_ready() and not self.client.is_closed()
        ok = database_ok and discord_ready

        return web.json_response(
            {
                "ok": ok,
                "discord_ready": discord_ready,
                "database_ok": database_ok,
                "uptime_seconds": int(time.time() - self.started_at),
                "monitor_last_success": self.monitor.last_success_at,
                "aoty_transport": HTTP.status(),
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

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/health", self._health)
        app.router.add_get("/live", self._live)

        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "0.0.0.0", PORT)
        await site.start()
        print(f"[HEALTH] HTTP :{PORT} /health")

    async def stop(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None
