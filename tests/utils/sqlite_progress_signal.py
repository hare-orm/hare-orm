from __future__ import annotations

import asyncio
from types import TracebackType
from typing import Any, Self

from hare.core.resource_completion import ResourceCompletion


class SqliteProgressSignal:
    """Signal the event loop after SQLite's worker actually starts executing a statement.

    Install after setup statements and before starting the operation under test. The
    callback never waits for the event loop and returns zero so SQLite keeps executing;
    the test must cancel and drain its operation before leaving this context.
    """

    def __init__(self, connection: Any) -> None:
        self.connection = connection
        self.loop = asyncio.get_running_loop()
        self.started = asyncio.Event()
        self.signalled = False

    def signal(self) -> int:
        if not self.signalled:
            self.signalled = True
            self.loop.call_soon_threadsafe(self.started.set)
        return 0

    async def __aenter__(self) -> Self:
        await self.connection.set_progress_handler(self.signal, 100)
        return self

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await ResourceCompletion.wait(self.connection.set_progress_handler(None, 0))
