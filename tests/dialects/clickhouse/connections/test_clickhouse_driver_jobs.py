"""Worker completion controls pool leases and stream termination."""

import asyncio
import threading
from types import SimpleNamespace

import pytest

from hare.dialects.base.client.pool.pool_statistics import PoolStatistics
from hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_connections import (
    ClickhouseDriverConnections,
)
from hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_row_stream import (
    ClickhouseDriverRowStream,
)
from hare.exceptions import DBConnectionError

pytestmark = pytest.mark.database_independent


class LibraryClient:
    def __init__(self, **settings):
        self.connection = SimpleNamespace(connected=True)
        self.disconnects = 0

    def disconnect(self):
        self.disconnects += 1
        self.connection.connected = False

    def execute_iter(self, sql, **options):
        yield [("number", "Int64")]
        for number in range(100):
            yield (number,)


@pytest.fixture
def library_clients(monkeypatch):
    clients = []

    def create(**settings):
        client = LibraryClient(**settings)
        clients.append(client)
        return client

    monkeypatch.setattr(
        "hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_connections."
        "ClickhouseDriverLibraryClient",
        create,
    )
    return clients


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_failed_handshake_is_counted_when_disconnect_also_fails(cleanup_fails):
    failure = ValueError("handshake")
    cleanup_failure = OSError("disconnect")
    disconnected = []

    def connect():
        raise failure

    def disconnect():
        disconnected.append(True)
        if cleanup_fails:
            raise cleanup_failure

    statistics = PoolStatistics()
    pool = ClickhouseDriverConnections(1, {}, statistics)
    client = SimpleNamespace(connection=SimpleNamespace(connect=connect, disconnect=disconnect))
    try:
        with pytest.raises((ValueError, OSError)) as caught:
            pool._connect(client)
        assert caught.value is (cleanup_failure if cleanup_fails else failure)
        if cleanup_fails:
            assert caught.value.__context__ is failure
        assert disconnected == [True]
        assert statistics.connect_failures == 1
        assert statistics.connect_count == 0
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_stream_startup_failure_terminates_without_a_queue_message(monkeypatch):
    def fail(**settings):
        raise ValueError("startup failed")

    monkeypatch.setattr(
        "hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_connections."
        "ClickhouseDriverLibraryClient",
        fail,
    )
    pool = ClickhouseDriverConnections(1, {}, PoolStatistics())
    try:
        stream = ClickhouseDriverRowStream(pool, "SELECT 1", 1).read_batches()
        with pytest.raises(ValueError, match="startup failed"):
            await anext(stream)
        assert not pool.running_statements
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_cancelled_waiter_keeps_lease_until_wire_completion(library_clients):
    pool = ClickhouseDriverConnections(1, {}, PoolStatistics())
    loop = asyncio.get_running_loop()
    started, release = asyncio.Event(), threading.Event()
    called = []

    def execute(client):
        loop.call_soon_threadsafe(started.set)
        release.wait()
        called.append(client)

    try:
        reading = pool.run(execute)
        await started.wait()
        queued = pool.submit(lambda client: called.append("queued"))
        assert queued.cancel()
        reading.cancel()
        assert len(pool.running_statements) == 1
        assert pool.busy_library_clients == {library_clients[0]}
    finally:
        release.set()
        await pool.close()
    assert called == library_clients
    assert library_clients[0].disconnects == 1
    assert not pool.running_statements and not pool.busy_library_clients
    with pytest.raises(DBConnectionError):
        pool.run(execute)
    await pool.close()
    assert library_clients[0].disconnects == 1


@pytest.mark.asyncio
async def test_stream_completion_drains_batches_and_early_close_releases_worker(library_clients):
    pool = ClickhouseDriverConnections(1, {}, PoolStatistics())
    try:
        complete = ClickhouseDriverRowStream(pool, "SELECT number", 7).read_batches()
        assert [row[0] async for batch in complete for row in batch] == list(range(100))
        assert library_clients[0].disconnects == 0
        stream = ClickhouseDriverRowStream(pool, "SELECT number", 1).read_batches()
        assert (await anext(stream))[0][0] == 0
        await stream.aclose()
        assert not pool.running_statements and not pool.busy_library_clients
        assert library_clients[0].disconnects == 1
    finally:
        await pool.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failures", [(0,), (0, 1), (0, 1, 2)])
async def test_close_attempts_every_idle_disconnect_and_keeps_failures(monkeypatch, failures):
    pool = ClickhouseDriverConnections(3, {}, PoolStatistics())
    clients = [LibraryClient() for _ in range(3)]
    errors = {index: OSError(f"disconnect {index}") for index in failures}
    attempted = []

    for index, client in enumerate(clients):

        def disconnect(index=index, client=client):
            attempted.append(index)
            client.disconnects += 1
            if index in errors:
                raise errors[index]

        monkeypatch.setattr(client, "disconnect", disconnect)
    pool.library_clients.extend(clients)
    error_type = OSError if len(errors) == 1 else ExceptionGroup
    with pytest.raises(error_type) as caught:
        await pool.close()
    assert attempted == [0, 1, 2]
    assert pool.is_closed and not pool.accepting
    assert not pool.library_clients
    collected = caught.value.exceptions if isinstance(caught.value, ExceptionGroup) else (caught.value,)
    assert collected == tuple(errors[index] for index in failures)
    with pytest.raises(error_type):
        await pool.close()
    assert attempted == [0, 1, 2]
