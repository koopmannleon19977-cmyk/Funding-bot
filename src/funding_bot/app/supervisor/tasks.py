"""
Task supervision helpers for restart/backoff behavior.
"""

from __future__ import annotations

import asyncio
from typing import Any

from funding_bot.domain.events import AlertEvent
from funding_bot.observability.logging import get_logger

logger = get_logger(__name__)


def _create_task(self, coro: Any, name: str) -> asyncio.Task:
    """Create a supervised task with proper exception handling."""
    task = asyncio.create_task(coro, name=name)
    task.add_done_callback(lambda t: self._handle_task_done(t, name))
    return task


def _handle_task_done(self, task: asyncio.Task, name: str) -> None:
    """Handle task completion/failure."""
    if task.cancelled():
        logger.debug(f"Task {name} cancelled")
        return

    exc = task.exception()

    # If shutdown is in progress, don't attempt restarts.
    if self._stopping or self._shutdown_event.is_set():
        if exc:
            logger.debug(f"Task {name} ended during shutdown: {exc}")
        return

    # These loops should not end under normal operation.
    if exc:
        logger.error(f"Task {name} failed with exception: {exc}", exc_info=exc)
        reason = f"exception: {type(exc).__name__}: {exc}"
    else:
        logger.error(f"Task {name} completed unexpectedly")
        reason = "unexpected completion"

    attempts = self._task_restart_attempts.get(name, 0) + 1
    self._task_restart_attempts[name] = attempts
    delay = min(60.0, 2.0 ** min(attempts, 6))  # 2s,4s,8s-> max 60s

    # Cancel any scheduled restart for this task (keep latest reason/delay).
    existing = self._task_restart_jobs.get(name)
    if existing and not existing.done():
        existing.cancel()

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No loop available (shouldn't happen in normal runtime); skip restart.
        return

    self._task_restart_jobs[name] = loop.create_task(
        self._restart_task_after_delay(name=name, delay_seconds=delay, reason=reason),
        name=f"restart_{name}",
    )


async def _restart_task_after_delay(self, name: str, delay_seconds: float, reason: str) -> None:
    """Restart a supervised task with backoff, if still running."""
    try:
        await asyncio.sleep(delay_seconds)

        if self._stopping or self._shutdown_event.is_set():
            return

        factory = self._task_factories.get(name)
        if not factory:
            logger.error(f"No task factory registered for {name}; cannot restart")
            return

        logger.warning(
            f"Restarting task {name} after {delay_seconds:.1f}s (reason={reason})"
        )

        if self.event_bus:
            await self.event_bus.publish(
                AlertEvent(
                    level="ERROR",
                    message=f"Task restarted: {name}",
                    details={
                        "delay_seconds": delay_seconds,
                        "attempts": self._task_restart_attempts.get(name, 0),
                        "reason": reason,
                    },
                )
            )

        self._tasks[name] = self._create_task(factory(), name=name)

    except asyncio.CancelledError:
        return
    except Exception as e:
        logger.exception(f"Failed to restart task {name}: {e}")


async def _cancel_all_tasks(self) -> None:
    """Cancel all running tasks safely."""
    if not self._tasks:
        # Still cancel any scheduled restart jobs
        for _, job in list(self._task_restart_jobs.items()):
            if not job.done():
                job.cancel()
        self._task_restart_jobs.clear()
        return

    logger.info(f"Cancelling {len(self._tasks)} tasks...")

    for _, task in self._tasks.items():
        if not task.done():
            task.cancel()

    # Cancel any scheduled restart jobs to avoid resurrecting loops during shutdown.
    for _, job in list(self._task_restart_jobs.items()):
        if not job.done():
            job.cancel()
    self._task_restart_jobs.clear()

    # Wait for all tasks to finish with timeout
    if self._tasks:
        results = await asyncio.gather(
            *self._tasks.values(),
            return_exceptions=True
        )

        # Log any unexpected exceptions (not CancelledError)
        for name, result in zip(self._tasks.keys(), results, strict=True):
            if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
                logger.error(f"Task {name} exception during cancel: {result}")

    self._tasks.clear()
