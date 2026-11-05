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
from hare.core.resource_completion import ResourceCompletion
from hare.exceptions import DBConnectionError
from hare.instrumentation.pools.pool_registry import PoolRegistry
from hare.transactions.transactions import Transactions
from tests.shared_models.testmodels import Tournament
from tests.utils.connection_acquisition_gate import ConnectionAcquisitionGate


@pytest.mark.sqlite_only
@pytest.mark.asyncio
async def test_a_sqlite_file_replaced_by_a_copy(tmp_path):
    live_path = tmp_path / "live.sqlite3"
    copy_path = tmp_path / "copy.sqlite3"
    async with hare_test_context(
        ["tests.shared_models.testmodels"],
        db_url=f"sqlite+aiosqlite:///{live_path}?synchronous=OFF",
        connection_label="models",
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
    killer_client = Connections.current().create_independent("models")
    try:
        async with killer_client._in_transaction() as killer:
            await killer.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = current_database() AND pid <> pg_backend_pid()"
            )
    finally:
        await killer_client.close()
    await Connections.reconnect()
    assert await Tournament.objects.filter(id=1).exists()
    assert connection is db_isolated.get_connection("models")


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_queries_running_through_a_reconnect_all_finish(db_isolated):
    """A query that had no connection yet when the pool closed failed - "pool is closing" with
    asyncpg, "Pool has been closed" with rust_pg - instead of running on the new pool."""
    await Tournament.objects.create(id=1, name="Spring")

    async def count(worker: int) -> int:
        return await Tournament.objects.count()

    for _ in range(3):
        gate = ConnectionAcquisitionGate()
        assert await gate.run(Connections.get("models"), count, Connections.reconnect) == [1] * gate.workers


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_a_connection_dropped_by_close_all_opens_no_pool_again(db_isolated):
    """A task still holding a connection close_all(discard=True) dropped opened a new pool on it,
    which nothing closed - its sessions stayed open on the server."""
    async with hare_test_context(
        ["tests.shared_models.testmodels"], db_url=os.environ["HARE_TEST_DB"], connection_label="models"
    ) as context:
        dropped = Connections.get("models")
        await Tournament.objects.count()
        await context.connections.close_all(discard=True)
        with pytest.raises(DBConnectionError, match="closed and dropped"):
            await dropped.execute("SELECT 1")
        assert dropped._pool is None


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_queries_running_through_a_repeated_init_all_finish(db_isolated):
    """A repeated init() with the same configuration closed the connections it replaced at once - a
    query that had taken one of them and had no pool connection yet failed with "closed and dropped",
    or "Pool has been closed" with rust_pg, instead of running on the new connection."""
    async with hare_test_context(
        ["tests.shared_models.testmodels"], db_url=os.environ["HARE_TEST_DB"], connection_label="models"
    ) as context:
        config = {
            "connections": {"models": context.connections.db_config["models"]},
            "apps": {"models": {"models": ["tests.shared_models.testmodels"], "default_connection": "models"}},
        }

        async def write(worker: int) -> int:
            tournament = await Tournament.objects.create(name=f"worker-{worker}")
            assert await Tournament.objects.filter(id=tournament.id).count() == 1
            return tournament.id

        async def reinitialize() -> None:
            await context.init(config)

        clients = [context.connections.get("models")]
        written: list[int] = []
        for _ in range(3):
            await Tournament.objects.count()
            written.extend(await ConnectionAcquisitionGate().run(clients[-1], write, reinitialize))
            clients.append(context.connections.get("models"))
        assert set(await Tournament.objects.values_list("id", flat=True)) == set(written)
        assert len(set(written)) == len(written)
        # A query taking a connection of a pool closing meanwhile listed the closed client as a pool
        # failing to open - the pool metrics went on counting it.
        registered = {id(client) for client in PoolRegistry.get_clients()}
        assert [client for client in clients if id(client) in registered and client.get_pool_occupancy() is None] == []


@pytest.mark.sqlite_only
@pytest.mark.asyncio
async def test_queries_running_through_a_reconnect_of_a_sqlite_file_all_finish(tmp_path):
    """A statement waiting for the SQLite connection while Connections.reconnect() closed it had taken
    the closed connection before it waited - "no active connection" instead of running on a new one."""
    async with hare_test_context(
        ["tests.shared_models.testmodels"],
        db_url=f"sqlite+aiosqlite:///{tmp_path / 'live.sqlite3'}?synchronous=OFF",
        connection_label="models",
    ):

        async def write(worker: int) -> int:
            tournament = await Tournament.objects.create(name=f"worker-{worker}")
            assert await Tournament.objects.filter(id=tournament.id).count() == 1
            return tournament.id

        written: list[int] = []
        for _ in range(3):
            await Tournament.objects.count()
            written.extend(
                await ConnectionAcquisitionGate().run(Connections.get("models"), write, Connections.reconnect)
            )
        assert set(await Tournament.objects.values_list("id", flat=True)) == set(written)
        assert len(set(written)) == len(written)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_a_pool_opened_while_a_close_waits_keeps_its_settings(db_isolated, monkeypatch):
    """A close still closing the tenant clients took the template of the pool a query opened meanwhile and
    cleared it - the opened pool then read its max_size from the empty template (KeyError on rust_pg)."""
    client = Tournament._meta.connection
    await client.close()
    client_type = type(client)
    close_tenant_clients = client_type.close_tenant_clients
    create_pool_with_retry = client_type._create_pool_with_retry
    close_entered = asyncio.Event()
    close_may_go_on = asyncio.Event()
    pool_may_open = asyncio.Event()
    pool_opening = asyncio.Event()

    async def close_tenant_clients_when_let(self):
        close_entered.set()
        await close_may_go_on.wait()
        await close_tenant_clients(self)

    async def create_pool_when_let(self, *args, **kwargs):
        pool_opening.set()
        await pool_may_open.wait()
        return await create_pool_with_retry(self, *args, **kwargs)

    monkeypatch.setattr(client_type, "close_tenant_clients", close_tenant_clients_when_let)
    monkeypatch.setattr(client_type, "_create_pool_with_retry", create_pool_when_let)
    tasks = [asyncio.create_task(client.close())]
    try:
        async with asyncio.timeout(30):
            await close_entered.wait()
            counting = asyncio.ensure_future(Tournament.objects.count())
            tasks.append(counting)
            await pool_opening.wait()
            close_may_go_on.set()
            await tasks[0]
            pool_may_open.set()
            assert await counting == 0
    finally:
        close_may_go_on.set()
        pool_may_open.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await ResourceCompletion.wait(asyncio.gather(*tasks, return_exceptions=True))
    monkeypatch.undo()
    assert await Tournament.objects.count() == 0


async def run_transactions_through_reconnects() -> None:
    """Reopen connections while transactions are waiting to acquire them, then verify committed rows."""

    async def transact(worker: int) -> int:
        async with Transactions.atomic():
            tournament = await Tournament.objects.create(name=f"worker-{worker}")
            await Tournament.objects.filter(id=tournament.id).update(desc="written")
            rows = [obj async for obj in Tournament.objects.filter(id=tournament.id).stream(chunk_size=1)]
            assert [(obj.id, obj.desc) for obj in rows] == [(tournament.id, "written")]
        return tournament.id

    written: list[int] = []
    for _ in range(3):
        await Tournament.objects.count()
        written.extend(
            await ConnectionAcquisitionGate().run(Connections.get("models"), transact, Connections.reconnect)
        )
    assert set(await Tournament.objects.values_list("id", flat=True)) == set(written)
    assert len(set(written)) == len(written)
    assert await Tournament.objects.filter(desc="written").count() == len(written)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_transactions_running_through_a_reconnect_all_finish(db_isolated):
    """asyncpg handed a transaction waiting for a connection one of the closing pool, which closed it under
    the transaction ("the underlying connection is closed", "unknown protocol state")."""
    await run_transactions_through_reconnects()


@pytest.mark.sqlite_only
@pytest.mark.asyncio
async def test_transactions_running_through_a_reconnect_of_a_sqlite_file_all_finish(tmp_path):
    """A transaction made before a close took the connection it found then - closed by the time it ran
    ("no active connection")."""
    async with hare_test_context(
        ["tests.shared_models.testmodels"],
        db_url=f"sqlite+aiosqlite:///{tmp_path / 'live.sqlite3'}?synchronous=OFF",
        connection_label="models",
    ):
        await run_transactions_through_reconnects()
