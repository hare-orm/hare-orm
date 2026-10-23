"""Two migrate runs of one SQLite database file applied the same migrations at once - the losers failed
with "table already exists". A run holds an OS lock on a file beside the database, the next one waits."""

import asyncio
from contextlib import nullcontext

import pytest

from hare.dialects.sqlite.drivers.aiosqlite.client.aiosqlite_client import AiosqliteClient
from tests.utils.waiting import WAIT_DEADLINE_SECONDS

pytestmark = pytest.mark.database_independent


@pytest.mark.asyncio
async def test_migrate_runs_of_one_database_file_run_one_after_another(tmp_path):
    database_path = str(tmp_path / "shared.sqlite3")
    clients = [AiosqliteClient(file_path=database_path, connection_alias="default") for _ in range(2)]
    order: list[str] = []
    first_holds, first_released = asyncio.Event(), asyncio.Event()

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
    # The second run gets every chance to start while the first holds the lock.
    for _ in range(100):
        await asyncio.sleep(0)
    first_released.set()
    await asyncio.wait_for(asyncio.gather(first, second), WAIT_DEADLINE_SECONDS)
    assert order == ["start 0", "end 0", "start 1", "end 1"]


def test_an_in_memory_database_takes_no_lock_file():
    client = AiosqliteClient(file_path=":memory:", connection_alias="default")
    assert isinstance(client.hold_migration_lock(), nullcontext)
