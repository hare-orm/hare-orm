"""Waiting for something a background task does - by the thing itself, not by a delay: a test's outcome
never depends on how fast the machine is, the deadline only stops a hung test."""

import asyncio
import inspect
from collections.abc import Callable

#: How long a condition may take before the wait gives up - a guard against a hang, never a measure.
WAIT_DEADLINE_SECONDS = 30
#: How often a condition is checked again.
WAIT_CHECK_INTERVAL_SECONDS = 0.01


async def wait_until(condition: Callable[[], object], deadline: float = WAIT_DEADLINE_SECONDS) -> None:
    """Waits until a condition holds.

    Args:
        condition: Checked again and again - a function or a coroutine function; a true result means it holds.
        deadline: The seconds after which the wait fails - only a hung test reaches it.

    Raises:
        AssertionError: The condition didn't hold before the deadline.
    """
    loop = asyncio.get_running_loop()
    give_up_at = loop.time() + deadline
    while True:
        holds = condition()
        if inspect.isawaitable(holds):
            holds = await holds
        if holds:
            return
        if loop.time() > give_up_at:
            raise AssertionError(f"the condition never held within {deadline}s")
        await asyncio.sleep(WAIT_CHECK_INTERVAL_SECONDS)
