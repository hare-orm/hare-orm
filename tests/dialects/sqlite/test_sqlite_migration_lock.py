"""Two migrate runs of one SQLite database file applied the same migrations at once - the losers failed
with "table already exists". A run holds an OS lock on a file beside the database, the next one waits."""

import asyncio
from contextlib import nullcontext

import pytest

from hare.dialects.sqlite.client.sqlite_migration_lock import SqliteMigrationLock
from hare.dialects.sqlite.drivers.aiosqlite.client.aiosqlite_client import AiosqliteClient
from tests.utils.migration_lock_assertions import MigrationLockAssertions
from tests.utils.waiting import WAIT_DEADLINE_SECONDS

pytestmark = pytest.mark.database_independent


@pytest.mark.asyncio
async def test_migrate_runs_of_one_database_file_run_one_after_another(tmp_path):
    database_path = str(tmp_path / "shared.sqlite3")
    clients = [AiosqliteClient(file_path=database_path, connection_alias="default") for _ in range(2)]
    first_released = asyncio.Event()
    try:
        await MigrationLockAssertions.assert_serialized(clients, first_released)
    finally:
        first_released.set()
        await asyncio.gather(*(client.close() for client in clients))


def test_an_in_memory_database_takes_no_lock_file():
    client = AiosqliteClient(file_path=":memory:", connection_alias="default")
    assert isinstance(client.hold_migration_lock(), nullcontext)


@pytest.mark.asyncio
@pytest.mark.parametrize("warm", [False, True], ids=["unopened", "opened"])
@pytest.mark.parametrize("address", ["symlink", "uri"])
async def test_database_aliases_share_the_native_migration_lock(tmp_path, monkeypatch, warm, address):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "shared.sqlite3"
    if address == "symlink":
        alias = tmp_path / "alias.sqlite3"
        try:
            alias.symlink_to(target)
        except OSError:
            pytest.skip("This environment cannot create symlinks")
        filenames = [str(target), str(alias)]
    else:
        filenames = ["file:shared.sqlite3?mode=rwc&cache=private", "file:shared.sqlite3?cache=private&mode=rwc"]
    clients = [
        AiosqliteClient(file_path=name, connection_alias=f"migration-{index}") for index, name in enumerate(filenames)
    ]
    attempted, entered = asyncio.Event(), asyncio.Event()
    attempts = []
    original = SqliteMigrationLock.try_lock
    contender = None

    def trying(cls, lock_file):
        locked = original(lock_file)
        if asyncio.current_task() is contender:
            attempts.append(locked)
            attempted.set()
        return locked

    monkeypatch.setattr(SqliteMigrationLock, "try_lock", classmethod(trying))

    async def take_second():
        async with clients[1].hold_migration_lock():
            entered.set()

    try:
        # The native filename independently establishes whether these spellings denote one DB.
        if warm:
            for client in clients:
                await client.execute_dicts("SELECT 1")
        async with clients[0].hold_migration_lock():
            contender = asyncio.create_task(take_second())
            await asyncio.wait_for(attempted.wait(), WAIT_DEADLINE_SECONDS)
            if address == "uri":
                native_paths = [(await client.execute_dicts("PRAGMA database_list"))[0]["file"] for client in clients]
                if native_paths[0] != native_paths[1]:
                    assert attempts[0] and entered.is_set()
                    return
            assert not attempts[0] and not entered.is_set()
        await asyncio.wait_for(contender, WAIT_DEADLINE_SECONDS)
        assert entered.is_set()
    finally:
        if contender is not None:
            await asyncio.gather(contender, return_exceptions=True)
        await asyncio.gather(*(client.close() for client in clients))
