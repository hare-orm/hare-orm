"""Connections.reconnect(): after the database was replaced under a running application, the next
query opens new connections and reads the replacement."""

from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from hare.contrib.test import requires_features
from hare.contrib.test.isolated_contexts import hare_test_context
from hare.core.connections.connections import Connections
from hare.exceptions import DBConnectionError
from tests.testmodels import Tournament


@pytest.mark.asyncio
async def test_a_sqlite_file_replaced_by_a_copy(tmp_path):
    live_path = tmp_path / "live.sqlite3"
    copy_path = tmp_path / "copy.sqlite3"
    async with hare_test_context(
        ["tests.testmodels"], db_url=f"sqlite+aiosqlite:///{live_path}", connection_label="models"
    ):
        await Tournament.objects.create(id=1, name="kept in the copy")
        # The file the connection opened - a parallel test run gives each worker its own name.
        live_path = Path(Connections.current().get("models").filename)
        await Connections.reconnect()
        shutil.copyfile(live_path, copy_path)
        await Tournament.objects.create(id=2, name="written after the copy")
        assert await Tournament.objects.all().count() == 2

        await Connections.reconnect()
        shutil.copyfile(copy_path, live_path)
        assert list(await Tournament.objects.all().values_list("name", flat=True)) == ["kept in the copy"]
        await Tournament.objects.create(id=3, name="written after the restore")
        assert await Tournament.objects.all().count() == 2
        with closing(sqlite3.connect(live_path)) as check:
            assert check.execute("SELECT count(*) FROM tournament").fetchone()[0] == 2


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_postgresql_connections_cut_by_a_restore(db_isolated):
    await Tournament.objects.create(id=1, name="Spring")
    connection = db_isolated.get_connection("models")
    async with Connections.current().create_independent("models")._in_transaction() as killer:
        await killer.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid()"
        )
    await Connections.reconnect()
    assert await Tournament.objects.filter(id=1).exists()
    assert connection is db_isolated.get_connection("models")


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_queries_running_through_a_reconnect_all_finish(db_isolated):
    """A query that had no connection yet when the pool closed failed - "pool is closing" with
    asyncpg, "Pool has been closed" with rust_pg - instead of running on the new pool."""
    await Tournament.objects.create(id=1, name="Spring")
    stop = asyncio.Event()
    errors: list[BaseException] = []

    async def query_until_stopped() -> None:
        while not stop.is_set():
            try:
                await Tournament.objects.count()
            except Exception as error:  # noqa: BLE001 - every failure is reported
                errors.append(error)
            await asyncio.sleep(0)

    workers = [asyncio.create_task(query_until_stopped()) for _ in range(8)]
    await asyncio.sleep(0.1)
    for _ in range(5):
        await Connections.reconnect()
        await asyncio.sleep(0.02)
    stop.set()
    await asyncio.gather(*workers)
    assert errors == []


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_a_connection_dropped_by_close_all_opens_no_pool_again(db_isolated):
    """A task still holding a connection close_all(discard=True) dropped opened a new pool on it,
    which nothing closed - its sessions stayed open on the server."""
    async with hare_test_context(
        ["tests.testmodels"], db_url=os.environ["HARE_TEST_DB"], connection_label="models"
    ) as context:
        dropped = Connections.get("models")
        await Tournament.objects.count()
        await context.connections.close_all(discard=True)
        with pytest.raises(DBConnectionError, match="closed and dropped"):
            await dropped.execute("SELECT 1")
        assert dropped._pool is None
