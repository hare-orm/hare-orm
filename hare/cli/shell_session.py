from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Awaitable
from contextlib import AbstractAsyncContextManager
from types import TracebackType
from typing import Self, TypeVar

from hare.cli.shell_launcher import ShellLauncher
from hare.core.hare_context import HareContext

TResult = TypeVar("TResult")


class ShellSession:
    """One event loop and application lifetime for all cells of an interactive shell.

    A lifecycle task enters and exits the application in the same context. Cells inherit its
    application frame but run as ordinary asyncio tasks. Synchronous IPython code runs outside
    the loop; its local loop runner starts that loop only while evaluating asynchronous code.
    No asyncio function, task implementation or event-loop policy is replaced.
    """

    def __init__(self, application: AbstractAsyncContextManager[HareContext]) -> None:
        self.application = application
        self.runner = asyncio.Runner()
        self.context: contextvars.Context
        self.hare_context: HareContext
        self.ready: asyncio.Future[None]
        self.stopped: asyncio.Event
        self.lifecycle: asyncio.Task[None]

    def __enter__(self) -> Self:
        self.runner.__enter__()
        try:
            self.runner.run(self._start())
        except BaseException:
            self.runner.close()
            raise
        return self

    async def _start(self) -> None:
        self.ready = asyncio.get_running_loop().create_future()
        self.stopped = asyncio.Event()
        self.lifecycle = asyncio.create_task(self._run_application())
        await self.ready

    async def _run_application(self) -> None:
        try:
            async with self.application as hare_context:
                self.hare_context = hare_context
                self.context = contextvars.copy_context()
                self.ready.set_result(None)
                await self.stopped.wait()
        except BaseException as error:
            if not self.ready.done():
                self.ready.set_exception(error)
            else:
                raise

    def run_cell(self, cell: Awaitable[TResult]) -> TResult:
        """Execute a cell on the session loop, with an independent copy of the application context."""
        return self.runner.run(self._await_cell(cell), context=self.context.copy())

    @staticmethod
    async def _await_cell(cell: Awaitable[TResult]) -> TResult:
        return await cell

    def launch(self) -> None:
        """Expose the same application to synchronous and asynchronous IPython cells."""
        self.context.run(
            ShellLauncher.launch_ipython,
            ShellLauncher.build_namespace(self.hare_context),
            self.run_cell,
        )

    async def _close(self) -> None:
        # Finish cell tasks before closing the pools they may still be using.
        current_task = asyncio.current_task()
        tasks = [task for task in asyncio.all_tasks() if task is not current_task and task is not self.lifecycle]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.stopped.set()
        await self.lifecycle

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            self.runner.run(self._close())
        finally:
            self.runner.close()
