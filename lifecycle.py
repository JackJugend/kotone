"""Small asyncio helpers shared by Kotone's graceful shutdown path."""

from __future__ import annotations

import asyncio
import os
import threading
from dataclasses import dataclass
from typing import Mapping


# Railway currently grants 15 seconds.  Keep one second for interpreter/process
# teardown after Kotone has closed Discord, SQLite and the health listener.
SHUTDOWN_TOTAL_SECONDS = 14.0
# Give active workers a short grace window, but reach the WAL checkpoint and
# backup early enough that a slow volume still has most of Railway's drain.
SHUTDOWN_BACKUP_RESERVE_SECONDS = 9.0
SHUTDOWN_CANCEL_SETTLE_SECONDS = 0.25
SHUTDOWN_HARD_EXIT_SECONDS = 14.5


@dataclass(frozen=True)
class WorkerStopResult:
    completed: tuple[str, ...]
    cancelled: tuple[str, ...]
    still_pending: tuple[str, ...]

    @property
    def forced(self) -> bool:
        return bool(self.cancelled or self.still_pending)


def arm_hard_exit_watchdog(
    delay: float = SHUTDOWN_HARD_EXIT_SECONDS,
    *,
    exit_code: int = 0,
) -> threading.Timer:
    """Guarantee container exit when Python waits for a stuck to_thread.

    ``asyncio.run`` normally waits for the default executor during teardown.
    A blocked synchronous HTTP request cannot be cancelled, so Railway would
    otherwise deliver SIGKILL at 15 seconds.  ``bot.py`` arms this only after a
    Railway shutdown signal, and the timer fires after normal cleanup had a
    chance to checkpoint/backup/close the database.
    """

    timer = threading.Timer(
        max(0.0, float(delay)),
        os._exit,
        args=(int(exit_code),),
    )
    timer.daemon = True
    timer.name = "kotone-railway-hard-exit"
    timer.start()
    return timer


async def persist_before_client_close(
    persistence,
    client_close,
    *,
    deadline: float,
    reserve_seconds: float = 0.0,
):
    """Finish the durable shutdown boundary before close may time out.

    Discord shutdown starts immediately, but it is never allowed to consume
    the time reserved for SQLite checkpoint/backup.  If ``client_close``
    hangs, its cancellation happens only after ``persistence`` completed.
    """

    close_task = asyncio.create_task(client_close)
    persistence_result = await persistence
    closed = await await_before_deadline(
        close_task,
        deadline=deadline,
        reserve_seconds=reserve_seconds,
    )
    return persistence_result, closed


def seconds_remaining(deadline: float, *, loop=None) -> float:
    loop = loop or asyncio.get_running_loop()
    return max(0.0, float(deadline) - loop.time())


async def stop_tasks_before_deadline(
    tasks: Mapping[str, asyncio.Task | None],
    *,
    deadline: float,
    reserve_seconds: float = SHUTDOWN_BACKUP_RESERVE_SECONDS,
) -> WorkerStopResult:
    """Wait for workers concurrently, then cancel them before DB finalization.

    A task cancelled while awaiting ``asyncio.to_thread`` stops at that await,
    so its coroutine cannot continue into the subsequent SQLite write.  Python
    cannot stop the underlying scraper thread; callers use ``forced`` to avoid
    closing a shared HTTP session that such a thread may still be using.
    """

    named = {
        name: task
        for name, task in tasks.items()
        if task is not None and not task.done()
    }
    already_cancelled = tuple(
        name
        for name, task in tasks.items()
        if task is not None and task.cancelled()
    )
    already_done = tuple(
        name
        for name, task in tasks.items()
        if task is not None and task.done() and not task.cancelled()
    )

    if not named:
        return WorkerStopResult(already_done, already_cancelled, ())

    loop = asyncio.get_running_loop()
    graceful_timeout = max(
        0.0,
        seconds_remaining(deadline, loop=loop) - max(0.0, reserve_seconds),
    )
    done, pending = await asyncio.wait(
        set(named.values()),
        timeout=graceful_timeout,
    )

    completed_names = list(already_done)
    completed_names.extend(
        name for name, task in named.items() if task in done
    )
    cancelled_names = list(already_cancelled)
    cancelled_names.extend(
        name for name, task in named.items() if task in pending
    )

    for task in pending:
        task.cancel()

    still_pending = set(pending)
    if pending:
        settle_timeout = min(
            SHUTDOWN_CANCEL_SETTLE_SECONDS,
            max(
                0.0,
                seconds_remaining(deadline, loop=loop)
                - max(0.0, reserve_seconds),
            ),
        )
        if settle_timeout > 0:
            _, still_pending = await asyncio.wait(
                pending,
                timeout=settle_timeout,
            )
        else:
            # Deliver Task.cancel() before the caller checkpoints/closes DB.
            # This consumes only one event-loop turn, not additional wall time.
            await asyncio.sleep(0)
            still_pending = {task for task in pending if not task.done()}

    return WorkerStopResult(
        tuple(completed_names),
        tuple(cancelled_names),
        tuple(
            name for name, task in named.items() if task in still_pending
        ),
    )


async def await_before_deadline(
    awaitable,
    *,
    deadline: float,
    reserve_seconds: float = 0.0,
) -> bool:
    """Await cleanup without extending the one shared shutdown deadline."""

    timeout = max(
        0.0,
        seconds_remaining(deadline) - max(0.0, reserve_seconds),
    )
    if timeout <= 0:
        if asyncio.iscoroutine(awaitable):
            awaitable.close()
        return False

    try:
        await asyncio.wait_for(awaitable, timeout=timeout)
        return True
    except (asyncio.TimeoutError, asyncio.CancelledError):
        return False
