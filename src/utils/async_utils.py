# src/utils/async_utils.py - Safe async utilities for shutdown handling
"""
Async utility functions for graceful shutdown handling.

These utilities prevent "exception was never retrieved" errors during shutdown
by properly handling CancelledError in asyncio.gather() and task cleanup.
"""

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger(__name__)


async def safe_gather(*coros: Coroutine, log_errors: bool = True) -> list[Any]:
    """
    A safer version of asyncio.gather that:
    1. Always uses return_exceptions=True
    2. Logs exceptions instead of raising them
    3. Returns successful results, None for failed tasks

    Usage:
        results = await safe_gather(task1(), task2(), task3())
    """
    results = await asyncio.gather(*coros, return_exceptions=True)

    processed = []
    for i, result in enumerate(results):
        if isinstance(result, asyncio.CancelledError):
            if log_errors:
                logger.debug(f"safe_gather: Task {i} was cancelled")
            processed.append(None)
        elif isinstance(result, Exception):
            if log_errors:
                logger.warning(f"safe_gather: Task {i} failed: {result}")
            processed.append(None)
        else:
            processed.append(result)

    return processed


async def cancel_task_safely(task: asyncio.Task, timeout: float = 2.0) -> None:
    """
    Cancel a task and retrieve its exception to prevent 'never retrieved' errors.

    Args:
        task: The asyncio.Task to cancel
        timeout: Maximum time to wait for task to finish after cancellation
    """
    if task is None:
        return

    if task.done():
        # Task already finished - retrieve exception to prevent "never retrieved" error
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug(f"Task {task.get_name()} had exception: {e}")
    else:
        # Task still running - cancel it
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=timeout)
        except TimeoutError:
            logger.debug(f"Task {task.get_name()} did not finish within timeout after cancel")
        except asyncio.CancelledError:
            pass  # Expected
        except Exception as e:
            logger.debug(f"Task {task.get_name()} exception during cancel: {e}")


def retrieve_task_exception(task: asyncio.Task) -> Exception | None:
    """
    Retrieve and return any exception from a done task.
    This prevents the "exception was never retrieved" warning.

    Returns:
        The exception if one occurred, None otherwise
    """
    if task is None or not task.done():
        return None

    try:
        task.result()
        return None
    except asyncio.CancelledError:
        return None  # Cancellation is expected during shutdown
    except Exception as e:
        return e


async def wait_for_tasks_with_cleanup(
    tasks: list[asyncio.Task], timeout: float = 5.0, cancel_pending: bool = True
) -> tuple:
    """
    Wait for tasks to complete and properly clean up exceptions.

    Args:
        tasks: List of tasks to wait for
        timeout: Maximum time to wait
        cancel_pending: Whether to cancel tasks that don't finish in time

    Returns:
        (done_tasks, pending_tasks) tuple
    """
    if not tasks:
        return set(), set()

    done, pending = await asyncio.wait(tasks, timeout=timeout)

    # Retrieve exceptions from done tasks to prevent "never retrieved" error
    for task in done:
        try:
            task.result()
        except asyncio.CancelledError:
            logger.debug(f"Task {task.get_name()} was cancelled (expected)")
        except Exception as e:
            logger.debug(f"Task {task.get_name()} had error: {e}")

    # Cancel pending tasks if requested
    if cancel_pending and pending:
        logger.debug(f"Cancelling {len(pending)} pending tasks...")
        for task in pending:
            await cancel_task_safely(task, timeout=1.0)

    return done, pending
