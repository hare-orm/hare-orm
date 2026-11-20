from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from kazoo.exceptions import NoAuthError, NodeExistsError, NoNodeError, NotEmptyError
from kazoo.protocol.states import KazooState

from hare.dialects.clickhouse.keeper.clickhouse_database_keeper_nodes import ClickhouseDatabaseKeeperNodes
from hare.dialects.clickhouse.keeper.clickhouse_migration_lock import ClickhouseMigrationLock
from hare.dialects.clickhouse.keeper.keeper_session import KeeperSession
from hare.exceptions import DBConnectionError, OperationalError

pytestmark = pytest.mark.database_independent


@pytest.fixture
def keeper_client(monkeypatch):
    listeners = set()
    client = SimpleNamespace(
        add_listener=listeners.add,
        remove_listener=listeners.discard,
        start=Mock(side_effect=lambda timeout: [listener(KazooState.CONNECTED) for listener in listeners]),
        stop=Mock(),
        close=Mock(),
        create=Mock(return_value="/lock"),
        delete=Mock(),
        get_children=Mock(return_value=["child"]),
        exists=Mock(return_value=None),
    )
    monkeypatch.setattr("kazoo.client.KazooClient", Mock(return_value=client))
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,error,expected",
    [
        ("create", None, True),
        ("create", NodeExistsError(), False),
        ("delete", None, None),
        ("delete", NoNodeError(), None),
        ("delete", NotEmptyError(), None),
        ("get_children", None, ["child"]),
        ("get_children", NoNodeError(), []),
        ("delete", NoAuthError(), OperationalError),
    ],
)
async def test_keeper_node_policies(keeper_client, method, error, expected):
    session = KeeperSession((("localhost", 9181),), 1)
    await session.open()
    getattr(keeper_client, method).side_effect = error
    try:
        if expected is OperationalError:
            with pytest.raises(OperationalError) as captured:
                await getattr(session, method)("/lock")
            assert captured.value.__cause__ is error
        else:
            assert await getattr(session, method)("/lock") == expected
        if method == "create":
            keeper_client.create.assert_called_once_with("/lock", ephemeral=True, makepath=True)
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_keeper_database_cleanup_preserves_request_and_release_failures(keeper_client):
    request_error = NoAuthError()
    stop_error, close_error = RuntimeError("stop"), RuntimeError("close")
    keeper_client.get_children.side_effect = request_error
    keeper_client.stop.side_effect = stop_error
    keeper_client.close.side_effect = close_error
    nodes = ClickhouseDatabaseKeeperNodes((("localhost", 9181),), "db", 1)
    with pytest.raises(ExceptionGroup) as captured:
        await nodes.delete("/series")
    request_failure, release_failure = captured.value.exceptions
    assert isinstance(request_failure, OperationalError)
    assert request_failure.__cause__ is request_error
    assert release_failure.exceptions == (stop_error, close_error)
    keeper_client.stop.assert_called_once()
    keeper_client.close.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [KazooState.SUSPENDED, KazooState.LOST])
async def test_keeper_session_loss_interrupts_lock_scope_without_reacquiring(keeper_client, state):
    lock = ClickhouseMigrationLock((("localhost", 9181),), "a/b", 1)
    interrupted = False
    with pytest.raises(DBConnectionError):
        async with lock.hold():
            session = lock.session
            session.state_changed(state)
            session.state_changed(KazooState.CONNECTED)
            try:
                await asyncio.Event().wait()
            finally:
                interrupted = True
    assert interrupted
    assert session.end_error is not None
    keeper_client.create.assert_called_once_with("/hare/migrations/a%2Fb/migrate", ephemeral=True, makepath=True)
    keeper_client.stop.assert_called_once()
    keeper_client.close.assert_called_once()
    assert lock.session is None


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
async def test_keeper_watch_from_library_thread_wakes_waiter(keeper_client, existing):
    session = KeeperSession((("localhost", 9181),), 1)
    await session.open()

    def exists(path, watch):
        assert path == "/lock"
        if existing:
            watch(SimpleNamespace(path=path))
            return object()
        return None

    keeper_client.exists.side_effect = exists
    try:
        await session.wait_for_deletion("/lock")
        assert not session._deletions
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_keeper_cancelled_operation_drains_worker_and_closes(keeper_client):
    session = KeeperSession((("localhost", 9181),), 1)
    await session.open()
    started, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    finished = []

    def get_children(path):
        loop.call_soon_threadsafe(started.set)
        release.wait()
        finished.append(path)
        return []

    keeper_client.get_children.side_effect = get_children
    keeper_client.stop.side_effect = release.set
    task = asyncio.create_task(session.get_children("/children"))
    try:
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        await session.close()
    assert finished == ["/children"]
    assert not session._workers
    keeper_client.stop.assert_called_once()
    keeper_client.close.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_keeper_scope_preserves_body_timeout_and_external_cancellation(keeper_client, cancel):
    entered = asyncio.Event()
    failure = TimeoutError("body")

    async def operation():
        async with ClickhouseMigrationLock((("localhost", 9181),), "db", 1).hold():
            entered.set()
            if cancel:
                await asyncio.Event().wait()
            raise failure

    task = asyncio.create_task(operation())
    await entered.wait()
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(TimeoutError) as captured:
            await task
        assert captured.value is failure
    keeper_client.stop.assert_called_once()
    keeper_client.close.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_keeper_start_failure_or_cancellation_closes_after_worker(keeper_client, cancel):
    session = KeeperSession((("localhost", 9181),), 1)
    started, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    failure = OSError("start failed")
    finished = []

    def start(timeout):
        loop.call_soon_threadsafe(started.set)
        release.wait()
        finished.append(True)
        if not cancel:
            raise failure

    keeper_client.start.side_effect = start
    task = asyncio.create_task(session.open())
    try:
        await started.wait()
        if cancel:
            task.cancel()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError if cancel else DBConnectionError) as captured:
        await task
    if not cancel:
        assert captured.value.__cause__ is failure
    assert finished == [True]
    keeper_client.stop.assert_called_once()
    keeper_client.close.assert_called_once()


@pytest.mark.asyncio
async def test_keeper_idle_loss_keeps_stop_and_close_errors_available(keeper_client):
    session = KeeperSession((("localhost", 9181),), 1)
    await session.open()
    failures = [OSError("stop failed"), OSError("close failed")]
    keeper_client.stop.side_effect = failures[0]
    keeper_client.close.side_effect = failures[1]
    session.receive_state(KazooState.LOST)
    with pytest.raises(ExceptionGroup) as captured:
        await session.close()
    assert list(captured.value.exceptions) == failures
    assert session._close_task.done()
    keeper_client.stop.assert_called_once()
    keeper_client.close.assert_called_once()


@pytest.mark.asyncio
async def test_keeper_scope_retains_body_and_release_errors(keeper_client):
    failure, release_error = ValueError("body"), OSError("stop")
    keeper_client.stop.side_effect = release_error
    with pytest.raises(ExceptionGroup) as captured:
        async with ClickhouseMigrationLock((("localhost", 9181),), "db", 1).hold():
            raise failure
    assert list(captured.value.exceptions) == [failure, release_error]
    keeper_client.close.assert_called_once()
