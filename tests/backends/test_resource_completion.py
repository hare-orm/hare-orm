from __future__ import annotations

import asyncio
import builtins
import threading
from types import SimpleNamespace

import pytest

from hare.contrib.notify.notification_listener import NotificationListener
from hare.core.connections.connection_handler import ConnectionHandler
from hare.core.hare_context import HareContext
from hare.dialects.base.client.password_provider import PasswordProvider
from hare.dialects.base.results import StatementResult
from hare.dialects.clickhouse.keeper.keeper_session import KeeperSession
from hare.dialects.postgresql.client.postgresql_client import PostgresqlClient
from hare.dialects.sqlite.client.sqlite_client import SqliteClient
from hare.exceptions import ConfigurationError
from hare.models.write.write_steps import WriteSteps
from tests.utils.fake_client import FakeClient

pytestmark = pytest.mark.database_independent


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_postgresql_shutdown_closes_all_pools_after_tenant_failure(cancel):
    client = FakeClient("postgresql")
    client._template = {"pool": "old"}
    entered, release = asyncio.Event(), asyncio.Event()
    closed = []
    failure = RuntimeError("tenant close failed")

    async def fail():
        closed.append("first")
        raise failure

    async def finish_tenant():
        entered.set()
        await release.wait()
        closed.append("second")

    async def finish_direct():
        closed.append("direct")

    async def finish_root():
        closed.append("root")

    client.tenant_clients.update(first=SimpleNamespace(close=fail), second=SimpleNamespace(close=finish_tenant))
    client._direct_client = SimpleNamespace(close=finish_direct)
    client._close = finish_root
    closing = asyncio.create_task(PostgresqlClient.close(client))
    await entered.wait()
    try:
        if cancel:
            closing.cancel()
        assert not closing.done()
    finally:
        release.set()
        outcomes = await asyncio.gather(closing, return_exceptions=True)
    assert set(closed) == {"first", "second", "direct", "root"}
    assert not client.tenant_clients
    assert client._direct_client is None
    assert client._template == {}
    if cancel:
        assert isinstance(outcomes[0], BaseExceptionGroup)
        assert isinstance(outcomes[0].exceptions[0], asyncio.CancelledError)
        assert outcomes[0].exceptions[1] is failure
    else:
        assert outcomes == [failure]


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["enable", "body"])
@pytest.mark.parametrize("original", [0, 1])
async def test_sqlite_constraint_scope_restores_setting_and_context_after_cancellation(phase, original):
    client = FakeClient("sqlite")
    client.pragmas = {"foreign_keys": original}
    editor = client.dialect.schema_editor_class(client)
    statements = []
    failure = asyncio.CancelledError("scope cancelled")
    previous_context = SqliteClient.foreign_keys_switched_off.get()

    async def execute(sql):
        statements.append(sql)
        if phase == "enable" and len(statements) == 1:
            raise failure
        return StatementResult(0, [])

    client.execute = execute
    with pytest.raises(asyncio.CancelledError) as raised:
        async with editor.constraint_checking_disabled():
            assert editor.foreign_key_check_required is bool(original)
            assert client.connection_alias in SqliteClient.foreign_keys_switched_off.get()
            raise failure
    assert raised.value is failure
    assert statements == ["PRAGMA foreign_keys = OFF", f"PRAGMA foreign_keys = {original}"]
    assert SqliteClient.foreign_keys_switched_off.get() == previous_context
    assert not editor.foreign_key_check_required


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_nontransactional_write_finishes_before_repeated_cancellation_propagates(fails):
    connection = SimpleNamespace(features=SimpleNamespace(supports_transactions=False))
    entered, release = asyncio.Event(), asyncio.Event()
    completed = []
    failure = RuntimeError("write failed")

    async def nested_write():
        assert WriteSteps.IN_WHOLE_RUN.get()
        return asyncio.current_task()

    async def write():
        task = asyncio.current_task()
        assert await WriteSteps.run_whole(connection, nested_write()) is task
        entered.set()
        await release.wait()
        completed.append(True)
        if fails:
            raise failure

    waiter = asyncio.create_task(WriteSteps.run_whole(connection, write()))
    await entered.wait()
    try:
        for _ in range(2):
            waiter.cancel()
            checkpoint = asyncio.Event()
            asyncio.get_running_loop().call_soon(checkpoint.set)
            await checkpoint.wait()
        assert not waiter.done()
    finally:
        release.set()
        outcomes = await asyncio.gather(waiter, return_exceptions=True)
    assert completed == [True]
    assert not WriteSteps.IN_WHOLE_RUN.get()
    if fails:
        assert isinstance(outcomes[0], BaseExceptionGroup)
        assert isinstance(outcomes[0].exceptions[0], asyncio.CancelledError)
        assert outcomes[0].exceptions[1] is failure
    else:
        assert isinstance(outcomes[0], asyncio.CancelledError)


@pytest.mark.asyncio
async def test_password_refresh_cleanup_survives_cancellation_of_stop():
    provider = PasswordProvider(lambda: "password", 60)
    started, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def refresh():
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            cleanup.set()
            await release.wait()

    worker = provider._refresh_task = asyncio.create_task(refresh())
    await started.wait()
    stopping = asyncio.create_task(provider.stop_refreshing())
    await cleanup.wait()
    stopping.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    assert worker.done()
    assert provider._refresh_task is None


@pytest.mark.asyncio
async def test_keeper_close_drains_transport_before_returning_cancellation():
    session = KeeperSession((), 1)
    closing, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    closed = []

    def stop():
        loop.call_soon_threadsafe(closing.set)
        release.wait()
        closed.append("stop")

    session._client = SimpleNamespace(
        remove_listener=lambda listener: None,
        stop=stop,
        close=lambda: closed.append("close"),
    )
    stopping = asyncio.create_task(session.close())
    try:
        await closing.wait()
        stopping.cancel()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    assert closed == ["stop", "close"]
    assert session._close_task.done()
    assert not session._workers
    await session.close()
    assert closed == ["stop", "close"]


@pytest.mark.asyncio
@pytest.mark.parametrize("different_context", [False, True])
async def test_application_shutdown_stops_listener_and_drains_its_connection(monkeypatch, different_context):
    application = HareContext()
    handler = application._connections = ConnectionHandler()
    closing, release = asyncio.Event(), asyncio.Event()
    closed = []

    async def close():
        closing.set()
        await release.wait()
        closed.append(True)

    async def listen(channel, callback):
        return SimpleNamespace(is_closed=lambda: False, close=close)

    client = FakeClient("postgresql")
    client.connection_handler = handler
    monkeypatch.setattr(client, "listen", listen, raising=False)
    listener = NotificationListener(client, "changes", lambda payload: None)
    ambient = HareContext() if different_context else application
    with ambient:
        await listener.start()
        assert listener in handler.resource_closers
        if different_context:
            assert listener not in ambient.connections.resource_closers
        stopping = asyncio.create_task(handler.close_all())
        await closing.wait()
        stopping.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await stopping
    assert closed == [True]
    assert not handler.resource_closers
    assert listener._run_task is None


@pytest.mark.asyncio
async def test_keeper_missing_extra_has_installation_error(monkeypatch):
    original_import = builtins.__import__

    def import_without_keeper(name, *args, **kwargs):
        if name == "kazoo.client":
            raise ModuleNotFoundError("kazoo")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_keeper)
    with pytest.raises(ConfigurationError, match=r"hare-orm\[clickhouse-keeper\]"):
        await KeeperSession((("localhost", 9181),), 1).open()
