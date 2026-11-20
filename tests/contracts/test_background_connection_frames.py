"""Background work detaches every inherited transaction without changing other context values."""

import asyncio
import contextvars

import pytest

from hare.contrib.test.databases.temporary_databases import TemporaryDatabases
from hare.core.connections.connection_handler import ConnectionHandler
from hare.core.connections.connections import Connections
from hare.core.hare_context import HareContext
from hare.instrumentation.declarations import QueryExecuted
from hare.instrumentation.observers.observers import Observers
from tests.utils.database_under_test import DatabaseUnderTest
from tests.utils.fake_client import FakeClient


@pytest.mark.asyncio
@pytest.mark.database_independent
@pytest.mark.parametrize("entrypoint", ("handler", "facade"))
async def test_background_tasks_detach_source_and_foreign_transaction_frames(monkeypatch, entrypoint):
    source, foreign = ConnectionHandler(), ConnectionHandler()
    source_client, foreign_client = FakeClient("sqlite"), FakeClient("sqlite")
    source_token = source.set("main", source_client)
    foreign_token = foreign.set("main", foreign_client)
    source_frame, foreign_frame = source.get_frame(), foreign.get_frame()
    retained_frame = contextvars.ContextVar("retained_frame", default=None)
    retained_token = retained_frame.set(source_frame)
    monkeypatch.setattr(HareContext, "get_current", classmethod(lambda cls: None))

    async def inspect_frames():
        await asyncio.sleep(0)
        return source.get_frame(), foreign.get_frame(), retained_frame.get()

    try:
        start = (
            source.create_task_outside_transactions
            if entrypoint == "handler"
            else Connections.create_task_outside_transactions
        )
        source_result, foreign_result, retained_result = await start(inspect_frames())
        assert source_result is source.empty_frame
        assert foreign_result is foreign.empty_frame
        assert retained_result is source_frame
        assert source.get_frame() is source_frame
        assert foreign.get_frame() is foreign_frame
        assert not source_token._used and not foreign_token._used
    finally:
        retained_frame.reset(retained_token)
        foreign_token.reset()
        source_token.reset()


@pytest.mark.asyncio
async def test_foreign_context_observer_write_survives_source_transaction_rollback():
    if DatabaseUnderTest.get_dialect().name != "postgresql":
        pytest.skip("A separate PostgreSQL session can write while the source transaction remains open")
    config = {"connections": {"main": None}, "apps": {}}
    async with TemporaryDatabases(
        config, DatabaseUnderTest.get_url(), generate_schemas=False, reuse_databases=False
    ) as source_context:
        source = source_context.get_connection("main")
        handler = source.connection_handler
        await source.execute_script("CREATE TABLE observer_rows (id INT PRIMARY KEY)")
        observed = asyncio.Event()
        transaction_flags = []

        async def persist_observation(event):
            if event.sql != "INSERT INTO observer_rows(id) VALUES(1)":
                return
            client = handler.get("main")
            transaction_flags.append(client.is_transaction_client)
            await client.execute_script("INSERT INTO observer_rows(id) VALUES(2)")
            observed.set()

        async with TemporaryDatabases(
            config, "sqlite+aiosqlite://:memory:", generate_schemas=False, reuse_databases=False
        ):
            with pytest.raises(RuntimeError, match="rollback the source write"):
                async with source._in_transaction() as transaction:
                    with Observers.observing(QueryExecuted, persist_observation):
                        await transaction.execute_script("INSERT INTO observer_rows(id) VALUES(1)")
                        await asyncio.wait_for(observed.wait(), 10)
                    raise RuntimeError("rollback the source write")
            await Observers.wait_for_pending()
            assert transaction_flags == [False]
            assert await source.execute_dicts("SELECT id FROM observer_rows ORDER BY id") == [{"id": 2}]
