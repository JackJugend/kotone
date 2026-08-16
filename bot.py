"""Kotone entry point.

The entry point deliberately contains almost no scraping/monitor logic:
- settings.py     -> config/assets/formats
- aoty.py         -> AOTY HTTP + parsing/search
- shared.py       -> variables shared by commands
- views.py        -> interactive Discord buttons/selects
- monitor.py      -> automatic + manual update checks
- commands/*.py   -> slash commands
"""

from __future__ import annotations

import asyncio

import discord

from commands.album import setup_album_command
from commands.artist import setup_artist_command
from commands.check import setup_check_command
from commands.last import setup_last_command
from commands.profile import setup_profile_command
from commands.recent import setup_recent_command
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

# Commands stay separated, but share settings/aoty/shared/views modules.
setup_last_command(tree)
setup_recent_command(tree)
setup_artist_command(tree)
setup_album_command(tree)
setup_profile_command(tree)
setup_check_command(tree, monitor)


async def setup_hook() -> None:
    """Guild-sync commands so new/changed commands appear immediately."""
    guild = discord.Object(id=GUILD_ID)

    # Force-remove the previous guild schema first. This is important after
    # deleting slash-command options (e.g. the old /artist "format" field),
    # because Discord can otherwise keep showing a stale command definition.
    tree.clear_commands(
        guild=guild
    )
    await tree.sync(
        guild=guild
    )

    # Commands are declared as global objects in discord.py and copied only to
    # the configured guild, so the fresh schema appears almost immediately.
    tree.copy_global_to(
        guild=guild
    )
    synced = await tree.sync(
        guild=guild
    )

    # Remove old global copies left by earlier Kotone versions.
    tree.clear_commands(
        guild=None
    )
    await tree.sync()

    print(
        f"[DISCORD] Zsynchronizowano {len(synced)} komend na serwerze."
    )
    for command in synced:
        print(f"[DISCORD] /{command.name}")


client.setup_hook = setup_hook
monitor_task: asyncio.Task | None = None


@client.event
async def on_ready() -> None:
    global monitor_task

    print(f"Zalogowano jako {client.user}")

    if monitor_task is None or monitor_task.done():
        monitor_task = asyncio.create_task(
            monitor.run(),
            name="kotone-aoty-monitor",
        )


client.run(TOKEN)
