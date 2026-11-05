from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from typing import Any

from hare.contrib.test.messages import TEST_RESOURCE_CLEANUP_FAILED
from hare.core.resource_completion import ResourceCompletion


class ResourceCleanup:
    """Drain a test scope's releases and retain every failure, including cancellation.

    Callbacks run in reverse registration order through ``AsyncExitStack``. A failed
    callback never skips another release. ContextVar tokens must be restored by the
    caller after ``close()``, because asynchronous releases run in a separate task.
    """

    def __init__(self) -> None:
        self.stack = AsyncExitStack()
        self.errors: list[BaseException] = []
        self.task: asyncio.Task[None] | None = None

    def push(self, callback: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> None:
        """Register a release immediately after its resource has been acquired."""
        self.stack.push_async_callback(self._release, callback, args, kwargs)

    async def _release(
        self, callback: Callable[..., Awaitable[Any]], args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> None:
        try:
            await callback(*args, **kwargs)
        except BaseException as error:
            self.errors.append(error)

    async def close(self) -> None:
        """Finish all releases once; keep failures for ``raise_errors()``.

        Repeated caller cancellation is recorded without interrupting an in-flight
        release or leaving later callbacks unexecuted.
        """
        if self.task is None:
            self.task = asyncio.create_task(self.stack.aclose())
        try:
            await ResourceCompletion.wait(self.task)
        except asyncio.CancelledError as error:
            self.errors.append(error)

    def raise_errors(self, original: BaseException | None = None) -> None:
        """Raise cleanup failures together with an original body/startup error.

        An original error alone belongs to the caller and is not raised here. One
        failure retains its type and identity; multiple failures form a group.
        """
        if not self.errors:
            return
        errors: list[BaseException] = []
        for error in (original, *self.errors):
            if error is not None and all(error is not previous for previous in errors):
                errors.append(error)
        if len(errors) == 1:
            raise errors[0]
        raise BaseExceptionGroup(TEST_RESOURCE_CLEANUP_FAILED, errors)
