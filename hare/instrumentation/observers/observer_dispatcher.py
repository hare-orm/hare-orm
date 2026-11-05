from __future__ import annotations

import asyncio
import contextvars
import inspect
import traceback
import weakref
from collections import deque
from functools import partial
from typing import TYPE_CHECKING, Any, ClassVar

from hare.core.connections.connections import Connections
from hare.core.log import logger
from hare.instrumentation.constants import MAX_PENDING_OBSERVER_DISPATCHES, MAX_STORED_OBSERVER_ERRORS

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from hare.instrumentation.observers.observer import Observer


class ObserverDispatcher:
    """Await callback results in one scope and event loop, outside the emitting transaction.

    One task per event waits for its predecessor before awaiting the event's callback
    results concurrently. The callback itself was called exactly once by ``notify``.
    A callback that starts a task controls when that task starts; the queue orders
    awaiting its result, not the execution of an already running task. Pending tasks,
    the backlog limit and retained errors belong only to this dispatcher.
    Cancelling one callback does not detach the others: a dispatch retains all
    callbacks until they finish, including cleanup after dispatch cancellation.
    """

    #: True inside an observer - the queries and transactions it runs report no events, so an
    #: observer that writes to the database can't trigger itself forever.
    inside_observer: ClassVar[contextvars.ContextVar[bool]] = contextvars.ContextVar(
        "hare_inside_observer", default=False
    )

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = weakref.ref(loop)
        self.pending_tasks: set[asyncio.Task[None]] = set()
        self.last_dispatch: asyncio.Task[None] | None = None
        self.dropped_dispatch_count = 0
        self.pending_errors: deque[Exception] = deque(maxlen=MAX_STORED_OBSERVER_ERRORS)
        self.dropped_error_count = 0

    def schedule(self, deliveries: list[tuple[Observer, Awaitable[None]]], event: Any) -> None:
        """Queue already-created awaitables once, disposing those the backlog refuses."""
        if not self.has_room():
            self.dispose(deliveries)
            return
        coroutine = self.run(deliveries, event, self.last_dispatch)
        try:
            task = Connections.create_task_outside_transactions(coroutine)
        except BaseException:
            coroutine.close()
            self.dispose(deliveries)
            raise
        self.last_dispatch = task
        self.pending_tasks.add(task)
        task.add_done_callback(partial(self.completed, deliveries))

    def completed(self, deliveries: list[tuple[Observer, Awaitable[None]]], task: asyncio.Task[None]) -> None:
        """Release the queue entry, including cancellation before its coroutine first runs."""
        self.pending_tasks.discard(task)
        if self.last_dispatch is task:
            self.last_dispatch = None
        self.dispose(deliveries)

    @staticmethod
    def dispose(deliveries: list[tuple[Observer, Awaitable[None]]]) -> None:
        """Close unstarted coroutines and cancel futures when their dispatch is discarded."""
        for _, result in deliveries:
            if inspect.iscoroutine(result):
                result.close()
            elif isinstance(result, asyncio.Future) and not result.done():
                result.cancel()

    def has_room(self) -> bool:
        """Whether another dispatch may start, warning once when the backlog fills up and once more
        when it drains."""
        if len(self.pending_tasks) >= MAX_PENDING_OBSERVER_DISPATCHES:
            if self.dropped_dispatch_count == 0:
                logger.warning(
                    "The observer backlog is full (%d dispatches pending) - new events are not delivered "
                    "to async observers until it drains; an observer may be stuck",
                    len(self.pending_tasks),
                )
            self.dropped_dispatch_count += 1
            return False
        if self.dropped_dispatch_count:
            logger.warning("The observer backlog drained - %d dispatches were dropped", self.dropped_dispatch_count)
            self.dropped_dispatch_count = 0
        return True

    async def run(
        self,
        deliveries: list[tuple[Observer, Awaitable[None]]],
        event: Any,
        previous_dispatch: asyncio.Future[None] | None,
    ) -> None:
        """One dispatch: waits for the previous one, then runs every observer together.

        Args:
            deliveries: The observers and the awaitables their callbacks returned.
            event: The event.
            previous_dispatch: The dispatch of the previous event on this loop.
        """
        # The task's copied context suppresses events produced by these callbacks.
        self.inside_observer.set(True)
        if previous_dispatch is not None and not previous_dispatch.done():
            await asyncio.wait([previous_dispatch])
        async with asyncio.TaskGroup() as callbacks:
            for observer, result in deliveries:
                callbacks.create_task(self.run_one(observer, result, event))

    async def run_one(self, observer: Observer, result: Awaitable[None], event: Any) -> None:
        """Runs one observer, logging and keeping its exception.

        Args:
            observer: The observer.
            result: The awaitable already returned by the callback.
            event: The event.
        """
        try:
            await result
        except Exception as error:
            logger.exception("Observer %r raised on %r", observer.callback, event)
            self.keep_error(error)

    def keep_error(self, error: Exception) -> None:
        """Keeps an observer's exception for ``wait_for_pending()``, dropping the oldest kept one
        past MAX_STORED_OBSERVER_ERRORS.

        Args:
            error: The exception.
        """
        self.clear_traceback_locals(error)
        if len(self.pending_errors) == self.pending_errors.maxlen:
            self.dropped_error_count += 1
        self.pending_errors.append(error)

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

    async def wait_for_pending(self, timeout_seconds: float | None = None) -> None:
        """Drain the current batch, then raise its retained observer errors.

        After the graceful timeout, remaining tasks are cancelled and their cleanup
        is awaited to completion. Callbacks must cooperate with cancellation. When
        invoked from another loop, draining is submitted to the dispatch loop.

        Args:
            timeout_seconds: The wait bound, None to wait as long as they run.

        Raises:
            Exception: The one observer failure, when exactly one observer raised.
            ExceptionGroup: Every kept failure, when two or more observers raised; its message
                counts the older ones that weren't kept.
        """
        loop = self.loop()
        if loop is not None and loop is not asyncio.get_running_loop() and loop.is_running():
            completion = asyncio.run_coroutine_threadsafe(self.wait_for_pending(timeout_seconds), loop)
            await asyncio.wrap_future(completion)
            return
        tasks = list(self.pending_tasks)
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
                await asyncio.gather(*still_running, return_exceptions=True)
        errors = list(self.pending_errors)
        self.pending_errors.clear()
        dropped_error_count, self.dropped_error_count = self.dropped_error_count, 0
        if len(errors) == 1 and not dropped_error_count:
            raise errors[0]
        if errors:
            message = "Observer failures"
            if dropped_error_count:
                message += f" ({dropped_error_count} older failures were logged but not kept)"
            raise ExceptionGroup(message, errors)
