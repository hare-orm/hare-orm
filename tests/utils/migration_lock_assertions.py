"""Assertions for migration locks shared by independent database clients."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import TYPE_CHECKING

from tests.utils.waiting import WAIT_DEADLINE_SECONDS

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient


class MigrationLockAssertions:
    @staticmethod
    async def assert_serialized(clients: Sequence[DatabaseClient], first_released: asyncio.Event) -> None:
        """Check that the second client enters only after the first leaves the lock."""
        order: list[str] = []
        first_holds = asyncio.Event()

        async def migrate(index: int) -> None:
            async with clients[index].hold_migration_lock():
                order.append(f"start {index}")
                if index == 0:
                    first_holds.set()
                    await first_released.wait()
                order.append(f"end {index}")

        first = asyncio.create_task(migrate(0))
        await first_holds.wait()
        second = asyncio.create_task(migrate(1))
        # Give the second task a chance to enter while the first holds the lock.
        for _ in range(100):
            await asyncio.sleep(0)
        first_released.set()
        await asyncio.wait_for(asyncio.gather(first, second), WAIT_DEADLINE_SECONDS)
        assert order == ["start 0", "end 0", "start 1", "end 1"]
