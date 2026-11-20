from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from tests.utils.waiting import wait_until

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient


async def assert_statement_waits_for_post_connect(
    client: DatabaseClient, has_published_resource: Callable[[], bool]
) -> None:
    """Check that another task cannot run a statement before post-connect setup finishes."""
    set_up = asyncio.Event()
    set_up_released = asyncio.Event()
    post_connect = client._post_connect

    async def slow_post_connect():
        await post_connect()
        await set_up_released.wait()
        set_up.set()

    client._post_connect = slow_post_connect
    try:
        opening = asyncio.create_task(client.execute_dicts("SELECT 1 AS n"))
        await wait_until(has_published_resource)
        set_up_seen: list[bool] = []

        async def other_statement() -> list[dict[str, Any]]:
            rows = await client.execute_dicts("SELECT 2 AS n")
            set_up_seen.append(set_up.is_set())
            return rows

        other = asyncio.create_task(other_statement())
        # The other statement gets every chance to run before the set-up ends.
        for _ in range(100):
            await asyncio.sleep(0)
        set_up_released.set()
        assert await other == [{"n": 2}]
        assert set_up_seen == [True]
        assert await opening == [{"n": 1}]
    finally:
        await client.close()
