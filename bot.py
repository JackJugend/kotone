"""Kotone entry point.

The entry point only wires modules together. Scraping policy, database logic,
monitoring and Discord views live in separate files so new features can be
added without turning bot.py into a monolith.
"""

from __future__ import annotations

import asyncio
import signal
import traceback

from runtime_guard import is_railway_runtime, validate_persistent_runtime

# This must run before importing settings/database through the modules below.
# Otherwise a broken Railway deployment could already create SQLite on the
# disposable filesystem before the guard gets a chance to stop startup.
validate_persistent_runtime()

import discord

from background import BackgroundWorker
from commands.album import setup_album_command
from commands.analytics import setup_analytics_commands
from commands.artist import setup_artist_command
from commands.check import setup_check_command
from commands.dbmanual import setup_dbmanual_command
from commands.dbonly import setup_dbonly_command
from commands.dbimport import setup_dbimport_command
from commands.dbstats import setup_dbstats_command
from commands.history import setup_history_command
from commands.manual import setup_manual_command
from commands.markov import setup_markov_command
from commands.last import setup_last_command
from commands.profile import setup_profile_command
from commands.rating_import import setup_rating_import_command
from commands.recent import setup_recent_command
from database import DB
from health import HealthServer
from http_client import HTTP
from lifecycle import (
    SHUTDOWN_BACKUP_RESERVE_SECONDS,
    SHUTDOWN_TOTAL_SECONDS,
    arm_hard_exit_watchdog,
    await_before_deadline,
    persist_before_client_close,
    stop_tasks_before_deadline,
)
from monitor import RatingMonitor
from markov_service import MarkovService
from presence_cache import PRESENCE_CACHE
from score_emojis import ScoreEmojiSynchronizer, StatusEmojiSynchronizer
from settings import APPLICATION_ID, GUILD_ID, TOKEN


intents = discord.Intents.default()
# /album without arguments reads the invoking member's Spotify or compatible
# Rich Presence from the Guild member cache. Both matching privileged intents
# must be enabled once in Discord Developer Portal before Discord sends and
# retains those activities for the bot.
intents.presences = True
intents.members = True
# Model Markowa uczy się z treści zwykłych wiadomości. Ten intent musi być
# również włączony w Discord Developer Portal dla aplikacji Kotone.
intents.message_content = True

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
health = HealthServer(client, monitor, background)
score_emojis = ScoreEmojiSynchronizer(client)
status_emojis = StatusEmojiSynchronizer(client)
markov = MarkovService(client)

setup_last_command(tree)
setup_recent_command(tree)
setup_artist_command(tree)
setup_album_command(tree)
setup_profile_command(tree)
setup_rating_import_command(tree)
setup_check_command(tree, monitor)
setup_dbonly_command(tree)
setup_dbimport_command(tree)
setup_dbmanual_command(tree)
setup_dbstats_command(tree)
setup_history_command(tree)
setup_manual_command(tree)
setup_analytics_commands(tree)
setup_markov_command(tree, markov)


@tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: discord.app_commands.AppCommandError,
) -> None:
    """Log every slash-command failure and give the invoker a safe answer.

    Without a tree-level handler discord.py can leave an interaction looking
    like it simply timed out while the useful traceback is easy to miss in
    Railway's stream.  The full cause stays only in the private service log.
    """

    original = getattr(error, "original", error)
    command = getattr(getattr(interaction, "command", None), "qualified_name", "?")
    print(f"[COMMAND] /{command}: {type(original).__name__}: {original}")
    traceback.print_exception(type(original), original, original.__traceback__)

    message = "❌ Wystąpił błąd komendy. Został zapisany w logach Kotone."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        # The traceback above is still enough to diagnose a failed response.
        pass


@client.event
async def on_presence_update(before: discord.Member, after: discord.Member) -> None:
    """Keep only the current in-memory activities for `/album` lookup."""
    PRESENCE_CACHE.update(after)


@client.event
async def on_message(message: discord.Message) -> None:
    """Ucz Markova wyłącznie na wiadomościach ludzi i obsłuż odpowiedzi."""

    try:
        await markov.handle_message(message)
    except discord.HTTPException as exc:
        print(f"[MARKOV] Nie wysłano odpowiedzi: HTTP {exc.status}.")
    except Exception as exc:
        print(f"[MARKOV] Obsługa wiadomości: {type(exc).__name__}: {exc}")


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
emoji_sync_task: asyncio.Task | None = None
markov_bootstrap_task: asyncio.Task | None = None
shutdown_deadline: float | None = None
shutdown_task: asyncio.Task | None = None
shutdown_snapshot_task: asyncio.Task | None = None
shutdown_watchdog = None
shutdown_requested = False
requested_exit_code = 0
shutdown_worker_result = None


def _log_worker_exit(task: asyncio.Task) -> None:
    if (
        task.cancelled()
        or client.is_closed()
        or shutdown_requested
    ):
        return
    try:
        error = task.exception()
    except (asyncio.CancelledError, RuntimeError):
        return

    if error is None:
        print(f"[WORKER] {task.get_name()} zatrzymał się niespodziewanie.")
    else:
        print(
            f"[WORKER] {task.get_name()} zakończył się błędem: "
            f"{type(error).__name__}: {error}"
        )

    # Railway checks /health only during deployment startup. A dead worker
    # later in the process lifetime therefore has to trigger ON_FAILURE itself
    # instead of waiting silently for an external health poll.
    _schedule_shutdown(exit_code=1)


@client.event
async def on_ready() -> None:
    global monitor_task, background_task, emoji_sync_task, markov_bootstrap_task

    print(f"Zalogowano jako {client.user}")

    if markov_bootstrap_task is None or markov_bootstrap_task.done():
        async def bootstrap_markov_history() -> None:
            try:
                await markov.bootstrap_history()
            except discord.Forbidden:
                print(
                    "[MARKOV] Brak dostępu do kanału lub uprawnienia "
                    "Read Message History."
                )
            except Exception as exc:
                print(f"[MARKOV] Import historii: {type(exc).__name__}: {exc}")

        markov_bootstrap_task = asyncio.create_task(
            bootstrap_markov_history(),
            name="kotone-markov-history",
        )

    if monitor_task is None or monitor_task.done():
        monitor_task = asyncio.create_task(
            monitor.run(),
            name="kotone-aoty-monitor",
        )
        monitor_task.add_done_callback(_log_worker_exit)

    if background_task is None or background_task.done():
        background_task = asyncio.create_task(
            background.run(),
            name="kotone-background-cache",
        )
        background_task.add_done_callback(_log_worker_exit)

    # Existing SQLite avatars can create their emoji immediately after a
    # deploy. This does not query AOTY; future comparisons are 7-day gated.
    try:
        await monitor.avatar_emojis.sync_cached()
    except Exception as exc:
        print(f"[AVATAR EMOJI] Startowy sync pominięty: {type(exc).__name__}: {exc}")

    # Cached custom emoji become usable instantly. Missing assets are created
    # afterwards in a detached, throttled task so Railway's startup health
    # check and Discord command sync never wait for 102 image uploads.
    score_emojis.load_cached()
    status_emojis.load_cached()
    if emoji_sync_task is None or emoji_sync_task.done():
        async def sync_presentation_emojis() -> None:
            try:
                await status_emojis.sync_all()
                await score_emojis.sync_all()
            except Exception as exc:
                print(f"[EMOJI] Sync prezentacji: {type(exc).__name__}: {exc}")

        emoji_sync_task = asyncio.create_task(
            sync_presentation_emojis(),
            name="kotone-presentation-emojis",
        )

    health.bind_worker_tasks(
        monitor_task=monitor_task,
        background_task=background_task,
    )


def _ensure_shutdown_deadline() -> float:
    global shutdown_deadline
    if shutdown_deadline is None:
        shutdown_deadline = (
            asyncio.get_running_loop().time()
            + SHUTDOWN_TOTAL_SECONDS
        )
    return shutdown_deadline


async def _persist_shutdown_snapshot():
    """Stop persistent workers, checkpoint WAL and create one final backup."""

    global shutdown_worker_result

    deadline = _ensure_shutdown_deadline()
    monitor.stop()
    background.stop()

    shutdown_worker_result = await stop_tasks_before_deadline(
        {
            "monitor": monitor_task,
            "background": background_task,
            "markov_history": markov_bootstrap_task,
        },
        deadline=deadline,
    )

    if shutdown_worker_result.forced:
        print(
            "[SHUTDOWN] Wymuszono zatrzymanie workerów: "
            + ", ".join(shutdown_worker_result.cancelled)
        )

    try:
        DB.checkpoint()
        DB.backup_if_due(force=True)
    except Exception as exc:
        print(f"[DB] Backup przy shutdown nie powiódł się: {exc}")

    return shutdown_worker_result


def _ensure_shutdown_snapshot_task() -> asyncio.Task:
    global shutdown_snapshot_task
    if shutdown_snapshot_task is None:
        shutdown_snapshot_task = asyncio.create_task(
            _persist_shutdown_snapshot(),
            name="kotone-shutdown-persistence",
        )
    return shutdown_snapshot_task


async def _request_shutdown() -> None:
    """Graceful SIGTERM/fatal-worker path with guaranteed DB snapshot."""

    global shutdown_watchdog

    deadline = _ensure_shutdown_deadline()
    if is_railway_runtime() and shutdown_watchdog is None:
        shutdown_watchdog = arm_hard_exit_watchdog(
            exit_code=requested_exit_code,
        )

    monitor.stop()
    background.stop()
    if not client.is_closed():
        _, closed = await persist_before_client_close(
            _ensure_shutdown_snapshot_task(),
            client.close(),
            deadline=deadline,
        )
        if not closed:
            print("[SHUTDOWN] Discord nie zamknął się przed deadline.")
    else:
        await _ensure_shutdown_snapshot_task()


def _schedule_shutdown(exit_code: int = 0) -> None:
    """Coalesce repeated SIGTERM/SIGINT into one shutdown coroutine."""

    global shutdown_task, shutdown_requested, requested_exit_code
    shutdown_requested = True
    requested_exit_code = max(requested_exit_code, int(exit_code))
    if shutdown_task is None or shutdown_task.done():
        shutdown_task = asyncio.create_task(
            _request_shutdown(),
            name="kotone-shutdown",
        )


async def main() -> None:
    loop = asyncio.get_running_loop()

    # Railway sends SIGTERM before SIGKILL. add_signal_handler is unavailable
    # on some Windows event loops, so local Windows development simply falls
    # back to normal discord.py/Ctrl+C behavior.
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(
                sig,
                _schedule_shutdown,
            )
        except (NotImplementedError, RuntimeError):
            pass

    await health.start()

    try:
        async with client:
            await client.start(TOKEN)
    finally:
        deadline = _ensure_shutdown_deadline()
        worker_result = await _ensure_shutdown_snapshot_task()

        await await_before_deadline(
            health.stop(),
            deadline=deadline,
        )

        # Cancelling asyncio.to_thread cannot stop its underlying function.
        # Do not close requests.Session underneath a scraper thread that may
        # still be finishing; process teardown/SIGKILL will reclaim it.
        if not worker_result.forced and not shutdown_requested:
            HTTP.close()
        else:
            print(
                "[SHUTDOWN] Pomijam HTTP.close(): request w tle może nadal "
                "kończyć nieanulowalny asyncio.to_thread."
            )

        # No await is allowed after closing SQLite. Any command coroutine that
        # survived Discord shutdown will be cancelled by asyncio.run before it
        # can resume and perform another DB operation.
        DB.close()
        markov.close()

    if requested_exit_code:
        raise SystemExit(requested_exit_code)


if __name__ == "__main__":
    asyncio.run(main())
