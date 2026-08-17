"""Kotone entry point.

The entry point only wires modules together. Scraping policy, database logic,
monitoring and Discord views live in separate files so new features can be
added without turning bot.py into a monolith.
"""

from __future__ import annotations

import asyncio
import signal

import discord

from background import BackgroundWorker
from commands.album import setup_album_command
from commands.artist import setup_artist_command
from commands.check import setup_check_command
from commands.dbstats import setup_dbstats_command
from commands.history import setup_history_command
from commands.last import setup_last_command
from commands.profile import setup_profile_command
from commands.recent import setup_recent_command
from database import DB
from health import HealthServer
from http_client import HTTP
from monitor import RatingMonitor
from settings import APPLICATION_ID, GUILD_ID, TOKEN


intents = discord.Intents.default()

activity = discord.Activity(
    type=discord.ActivityType.watching,
    name="AOTY.org",
)

client = discord.Client(
    intents=intents,
    application_id=APPLICATION_ID,
    activity=activity,
    status=discord.Status.idle,
)

tree = discord.app_commands.CommandTree(client)
monitor = RatingMonitor(client)
background = BackgroundWorker(client)
health = HealthServer(client, monitor)

setup_last_command(tree)
setup_recent_command(tree)
setup_artist_command(tree)
setup_album_command(tree)
setup_profile_command(tree)
setup_check_command(tree, monitor)
setup_dbstats_command(tree)
setup_history_command(tree)


async def setup_hook() -> None:
    """Sync one authoritative guild command set.

    Clearing locally and syncing only after commands are copied avoids the old
    two-step deploy where the server briefly had zero slash commands.
    """

    guild = discord.Object(id=GUILD_ID)
    tree.clear_commands(guild=guild)
    tree.copy_global_to(guild=guild)
    synced = await tree.sync(guild=guild)

    # Old global copies from historic Kotone versions should stay removed.
    tree.clear_commands(guild=None)
    await tree.sync()

    print(f"[DISCORD] Zsynchronizowano {len(synced)} komend na serwerze.")
    for command in synced:
        print(f"[DISCORD] /{command.name}")


client.setup_hook = setup_hook
monitor_task: asyncio.Task | None = None
background_task: asyncio.Task | None = None


@client.event
async def on_ready() -> None:
    global monitor_task, background_task

    print(f"Zalogowano jako {client.user}")

    if monitor_task is None or monitor_task.done():
        monitor_task = asyncio.create_task(
            monitor.run(),
            name="kotone-aoty-monitor",
        )

    if background_task is None or background_task.done():
        background_task = asyncio.create_task(
            background.run(),
            name="kotone-background-cache",
        )


async def _request_shutdown() -> None:
    """Graceful SIGTERM path used by Railway draining."""

    monitor.stop()
    background.stop()
    if not client.is_closed():
        await client.close()


async def main() -> None:
    loop = asyncio.get_running_loop()

    # Railway sends SIGTERM before SIGKILL. add_signal_handler is unavailable
    # on some Windows event loops, so local Windows development simply falls
    # back to normal discord.py/Ctrl+C behavior.
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(
                sig,
                lambda: asyncio.create_task(_request_shutdown()),
            )
        except (NotImplementedError, RuntimeError):
            pass

    await health.start()

    try:
        async with client:
            await client.start(TOKEN)
    finally:
        monitor.stop()
        background.stop()

        if monitor_task is not None and not monitor_task.done():
            try:
                await asyncio.wait_for(monitor_task, timeout=8)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                monitor_task.cancel()

        if background_task is not None and not background_task.done():
            try:
                await asyncio.wait_for(background_task, timeout=8)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                background_task.cancel()

        await health.stop()

        # Flush WAL and keep one local SQLite backup on the persistent volume.
        try:
            DB.backup_if_due(force=True)
        except Exception as exc:
            print(f"[DB] Backup przy shutdown nie powiódł się: {exc}")
        HTTP.close()
        DB.close()


if __name__ == "__main__":
    asyncio.run(main())
