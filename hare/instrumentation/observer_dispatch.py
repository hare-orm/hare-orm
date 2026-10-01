from __future__ import annotations

import asyncio
import contextvars
import math
import traceback
import weakref
from collections import deque
from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.core.connections import Connections
from hare.core.log import logger
from hare.exceptions import QueryError
from hare.instrumentation.constants import MAX_PENDING_OBSERVER_DISPATCHES, MAX_STORED_OBSERVER_ERRORS

if TYPE_CHECKING:  # pragma: nocoverage
    from collections.abc import Awaitable, Callable

    from hare.instrumentation.observer import Observer


class ObserverDispatch:
    """Runs ``async def`` observers in the background - an event never waits for them. One task per
    event runs its observers together; on one event loop, an event's observers start only after the
    previous event's have finished, so observers see events in the order they happened. The task
    runs outside every transaction of the calling context: an observer's own queries never join (or
    are rolled back with) the transaction that triggered them, and report no events of their own.
    """

    #: True inside an observer - the queries and transactions it runs report no events, so an
    #: observer that writes to the database can't trigger itself forever.
    inside_observer: ClassVar[contextvars.ContextVar[bool]] = contextvars.ContextVar(
        "hare_inside_observer", default=False
    )

    #: Every dispatch in flight - a strong reference keeps asyncio from collecting one mid-flight.
    pending_tasks: ClassVar[set[asyncio.Task[None]]] = set()

    #: Dispatches dropped since the backlog last had room.
    dropped_dispatch_count: ClassVar[int] = 0

    #: The latest dispatch per event loop - the next one waits for it.
    last_dispatch_by_loop: ClassVar[weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Future[None]]] = (
        weakref.WeakKeyDictionary()
    )

    #: The latest MAX_STORED_OBSERVER_ERRORS exceptions of background observers since the last
    #: ``wait_for_pending()``, their traceback frames' locals cleared.
    pending_errors: ClassVar[deque[Exception]] = deque(maxlen=MAX_STORED_OBSERVER_ERRORS)

    #: Exceptions pushed out of ``pending_errors`` since the last ``wait_for_pending()``.
    dropped_error_count: ClassVar[int] = 0

    @classmethod
    def schedule(cls, observers: list[Observer], event: Any) -> None:
        """Starts a background dispatch of ``event`` to ``observers`` - dropped with a warning when
        MAX_PENDING_OBSERVER_DISPATCHES are already pending (an observer may be stuck).

        Args:
            observers: The ``async def`` observers.
            event: The event.
        """
        if not cls.has_room():
            return
        loop = asyncio.get_running_loop()
        previous_dispatch = cls.last_dispatch_by_loop.get(loop)
        task = Connections.create_task_outside_transactions(cls.run(observers, event, previous_dispatch))
        cls.last_dispatch_by_loop[loop] = task
        cls.pending_tasks.add(task)
        task.add_done_callback(cls.pending_tasks.discard)

    @classmethod
    def has_room(cls) -> bool:
        """Whether another dispatch may start, warning once when the backlog fills up and once more
        when it drains."""
        if len(cls.pending_tasks) >= MAX_PENDING_OBSERVER_DISPATCHES:
            if cls.dropped_dispatch_count == 0:
                logger.warning(
                    "The observer backlog is full (%d dispatches pending) - new events are not delivered "
                    "to async observers until it drains; an observer may be stuck",
                    len(cls.pending_tasks),
                )
            cls.dropped_dispatch_count += 1
            return False
        if cls.dropped_dispatch_count:
            logger.warning("The observer backlog drained - %d dispatches were dropped", cls.dropped_dispatch_count)
            cls.dropped_dispatch_count = 0
        return True

    @classmethod
    async def run(cls, observers: list[Observer], event: Any, previous_dispatch: asyncio.Future[None] | None) -> None:
        """One dispatch: waits for the previous one, then runs every observer together.

        Args:
            observers: The observers.
            event: The event.
            previous_dispatch: The dispatch of the previous event on this loop.
        """
        # The task runs in its own copy of the context - the flag covers only the observers.
        cls.inside_observer.set(True)
        if previous_dispatch is not None and not previous_dispatch.done():
            await asyncio.wait([previous_dispatch])
        await asyncio.gather(*(cls.run_one(observer, event) for observer in observers))

    @classmethod
    async def run_one(cls, observer: Observer, event: Any) -> None:
        """Runs one observer, logging and keeping its exception.

        Args:
            observer: The observer.
            event: The event.
        """
        try:
            await cast("Callable[[Any], Awaitable[None]]", observer.callback)(event)
        except Exception as error:
            logger.exception("Observer %r raised on %r", observer.callback, event)
            cls.keep_error(error)

    @classmethod
    def keep_error(cls, error: Exception) -> None:
        """Keeps an observer's exception for ``wait_for_pending()``, dropping the oldest kept one
        past MAX_STORED_OBSERVER_ERRORS.

        Args:
            error: The exception.
        """
        cls.clear_traceback_locals(error)
        if len(cls.pending_errors) == cls.pending_errors.maxlen:
            cls.dropped_error_count += 1
        cls.pending_errors.append(error)

    @staticmethod
    def clear_traceback_locals(error: BaseException) -> None:
        """Clears the local variables of every finished frame in the tracebacks of ``error`` and
        the exceptions chained to it, so a kept exception doesn't keep the observer's data alive.

        Args:
            error: The exception.
        """
        seen_error_ids: set[int] = set()
        pending_errors: list[BaseException | None] = [error]
        while pending_errors:
            current_error = pending_errors.pop()
            if current_error is None or id(current_error) in seen_error_ids:
                continue
            seen_error_ids.add(id(current_error))
            traceback.clear_frames(current_error.__traceback__)
            pending_errors.extend((current_error.__cause__, current_error.__context__))
            if isinstance(current_error, BaseExceptionGroup):
                pending_errors.extend(current_error.exceptions)

    @classmethod
    async def wait_for_pending(cls, timeout_seconds: float | None = None) -> None:
        """Waits for the dispatches in flight right now - past ``timeout_seconds``, cancels the ones
        still running with a warning - then re-raises what background observers raised since the
        last call (each was logged when it happened already).

        Args:
            timeout_seconds: The wait bound, None to wait as long as they run.

        Raises:
            ValueError: ``timeout_seconds`` is not a positive number.
            Exception: The one observer failure, when exactly one observer raised.
            ExceptionGroup: Every kept failure, when two or more observers raised; its message
                counts the older ones that weren't kept.
        """
        if timeout_seconds is not None and (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int | float)
            or not 0 < timeout_seconds < math.inf
        ):
            raise QueryError(f"timeout_seconds must be a positive number, got {timeout_seconds!r}")
        tasks = list(cls.pending_tasks)
        if tasks:
            __, still_running = await asyncio.wait(tasks, timeout=timeout_seconds)
            if still_running:
                logger.warning(
                    "%d observer dispatches still running after %.1fs - cancelling them",
                    len(still_running),
                    timeout_seconds,
                )
                for task in still_running:
                    task.cancel()
                await asyncio.wait(still_running, timeout=timeout_seconds)
        errors = list(cls.pending_errors)
        cls.pending_errors.clear()
        dropped_error_count, cls.dropped_error_count = cls.dropped_error_count, 0
        if len(errors) == 1 and not dropped_error_count:
            raise errors[0]
        if errors:
            message = "Observer failures"
            if dropped_error_count:
                message += f" ({dropped_error_count} older failures were logged but not kept)"
            raise ExceptionGroup(message, errors)
