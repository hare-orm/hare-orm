from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from hare.core.constants import RESOURCE_COMPLETION_FAILED

TResult = TypeVar("TResult")


class ResourceCompletion:
    """Finish an already-started release before propagating caller cancellation.

    The operation retains its resources until completion. Repeated cancellation of the
    waiter cannot detach cleanup; an operation error remains available on its future.
    This helper does not retry IO or infer the outcome of a database statement.
    """

    @classmethod
    async def wait_all(cls, *operations: Awaitable[object]) -> None:
        """Finish every independent release, then propagate cancellation and all failures.

        One failure retains its identity; multiple failures form an exception group. Caller
        cancellation cannot orphan siblings or discard errors produced during their release.
        """
        await cls.wait(cls._collect_all(operations))

    @staticmethod
    async def _collect_all(operations: tuple[Awaitable[object], ...]) -> None:
        results = await asyncio.gather(*operations, return_exceptions=True)
        errors = [result for result in results if isinstance(result, BaseException)]
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup(RESOURCE_COMPLETION_FAILED, errors)

    @staticmethod
    async def wait(
        operation: Awaitable[TResult], *, on_cancel: Callable[[asyncio.CancelledError], None] | None = None
    ) -> TResult:
        """Drain the operation, notifying its controller when the waiter is cancelled.

        ``on_cancel`` may stop later work in a multi-resource release; it must not raise or
        cancel the operation being drained. Already submitted IO is never retried here.
        """
        task = asyncio.ensure_future(operation)
        cancellation: asyncio.CancelledError | None = None
        while not task.done():
            try:
                # Cancelling asyncio.wait() leaves the operation itself running.
                # Its result is retrieved exactly once below, including a failure
                # after the waiter was cancelled.
                await asyncio.wait((task,))
            except asyncio.CancelledError as error:
                cancellation = error
                if on_cancel is not None:
                    on_cancel(error)
        if cancellation is not None:
            if not task.cancelled():
                failure = task.exception()
                if failure is not None:
                    raise BaseExceptionGroup(RESOURCE_COMPLETION_FAILED, [cancellation, failure])
            raise cancellation
        return task.result()
