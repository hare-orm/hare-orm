from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from hare.core.connections.connections import Connections
from hare.core.resource_completion import ResourceCompletion
from tests.utils.waiting import WAIT_DEADLINE_SECONDS, wait_until


class PostgresqlQueryCancellation:
    """Cancel a real wire operation only after the server reports it inside pg_sleep.

    A separate client observes a unique transaction-local application name. The target
    can therefore use a pool of one connection or an already acquired transaction. The
    sleep keeps the operation in flight; neither its duration nor a delay schedules the
    cancellation. The target task and observer are always drained and closed.
    """

    @staticmethod
    async def cancel(
        operation: Callable[[str], Awaitable[Any]],
        *,
        connection_alias: str = "models",
    ) -> None:
        """Cancel at the server boundary and drain the task through the driver's cancellation."""
        observer = Connections.current().create_independent(connection_alias)
        marker = f"cancellation_{uuid.uuid4().hex}"
        # Native cursors execute FETCH, so pg_stat_activity no longer contains the
        # original SELECT. A transaction-local application name identifies its backend.
        sql = (
            "SELECT pg_sleep(60) FROM "
            f"(SELECT set_config('application_name', '{marker}', true) AS name) cancellation "
            f"WHERE name = '{marker}'"
        )
        task = None
        try:
            await observer.execute("SELECT 1")
            task = asyncio.ensure_future(operation(sql))

            async def is_executing() -> bool:
                if task.done():
                    await task
                    raise AssertionError("The statement finished before reaching the cancellation boundary")
                rows = await observer.execute_dicts(
                    "SELECT pid FROM pg_stat_activity WHERE datname = current_database() "
                    "AND pid <> pg_backend_pid() AND state = 'active' AND wait_event = 'PgSleep' "
                    "AND application_name = $1",
                    [marker],
                )
                return len(rows) == 1

            async with asyncio.timeout(WAIT_DEADLINE_SECONDS):
                await wait_until(is_executing)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        finally:
            try:
                if task is not None:
                    if not task.done():
                        task.cancel()
                    await ResourceCompletion.wait(asyncio.gather(task, return_exceptions=True))
            finally:
                await ResourceCompletion.wait(observer.close())
