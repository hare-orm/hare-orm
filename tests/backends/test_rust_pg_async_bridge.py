"""Native futures complete on their caller's loop and release closed-loop references."""

import asyncio
import contextvars
import gc
import threading
import weakref

import pytest

from hare import Connections
from hare.contrib.test import requires_features
from hare.dialects.postgresql.drivers.rust_pg.client import RustPgClient
from hare.native.native_modules import NativeModules
from tests.utils.waiting import wait_until


async def query_on_a_new_client(weak_loops):
    loop = asyncio.get_running_loop()
    weak_loops.append(weakref.ref(loop))
    client = Connections.current().create_independent("models", {"min_size": 1, "max_size": 1})
    marker = contextvars.ContextVar("native_callback_context", default="unset")
    marker.set("caller")
    try:
        assert await client.execute_dicts("SELECT 1 AS one") == [{"one": 1}]
        future = client._pool.fetch_all("SELECT 2 AS two", [])
        assert future.get_loop() is loop
        completed = asyncio.Event()
        callbacks = []
        thread_id = threading.get_ident()

        def callback(result):
            callbacks.append((threading.get_ident(), marker.get()))
            completed.set()

        future.add_done_callback(callback)
        marker.set("later")
        await future
        await completed.wait()
        assert callbacks == [(thread_id, "caller")]
    finally:
        await client.close()


def run_loops(context, weak_loops):
    for _ in range(5):
        context.run(asyncio.run, query_on_a_new_client(weak_loops))


def loops_collected(weak_loops):
    # Flush PyO3 references dropped by worker threads before Python's GC pass.
    NativeModules.clock.get_posix_time()
    gc.collect()
    return all(reference() is None for reference in weak_loops)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_native_future_callbacks_and_closed_loop_collection(db_simple):
    if not isinstance(Connections.get("models"), RustPgClient):
        pytest.skip("native PostgreSQL bridge")
    weak_loops = []
    await asyncio.to_thread(run_loops, contextvars.copy_context(), weak_loops)
    assert len(weak_loops) == 5
    await wait_until(lambda: loops_collected(weak_loops))


def close_loop_before_native_start(context, weak_loops):
    """Close in the callback creating the future, before the batched start callback can run."""
    loop = asyncio.new_event_loop()
    weak_loops.append(weakref.ref(loop))
    pending = []

    async def open_client():
        client = Connections.current().create_independent("models", {"min_size": 1, "max_size": 1})
        await client.execute_dicts("SELECT 1 AS one")
        return client

    client = context.run(loop.run_until_complete, open_client())

    def queue_and_stop():
        # A caller context can legitimately contain its loop. Nothing native may root it.
        marker = contextvars.ContextVar("native_start_loop")
        marker.set(loop)
        pending.append(client._pool.fetch_all("SELECT 2 AS two", []))
        loop.stop()

    try:
        loop.call_soon(queue_and_stop)
        loop.run_forever()
    finally:
        loop.close()
    pending.clear()
    return client


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_closing_before_batched_start_releases_the_loop(db_simple):
    if not isinstance(Connections.get("models"), RustPgClient):
        pytest.skip("native PostgreSQL bridge")
    weak_loops = []
    client = await asyncio.to_thread(close_loop_before_native_start, contextvars.copy_context(), weak_loops)
    await client.close()
    del client
    await wait_until(lambda: loops_collected(weak_loops))


async def start_pending_query(weak_loops):
    weak_loops.append(weakref.ref(asyncio.get_running_loop()))
    client = Connections.current().create_independent("models", {"min_size": 1, "max_size": 1})
    assert await client.execute_dicts("SELECT 1 AS one") == [{"one": 1}]
    return client, client._pool.fetch_all("SELECT pg_sleep(0.3)", [])


def close_loop_before_completion(context, weak_loops):
    loop = asyncio.new_event_loop()
    try:
        client, pending = context.run(loop.run_until_complete, start_pending_query(weak_loops))
    finally:
        loop.close()
    del client, pending


@requires_features(dialect="postgresql")
@pytest.mark.timing
@pytest.mark.asyncio
async def test_completion_after_forced_loop_close_releases_bridge_references(db_simple, capsys):
    if not isinstance(Connections.get("models"), RustPgClient):
        pytest.skip("native PostgreSQL bridge")
    weak_loops = []
    await asyncio.to_thread(close_loop_before_completion, contextvars.copy_context(), weak_loops)
    await wait_until(lambda: loops_collected(weak_loops))
    assert "Event loop is closed" not in capsys.readouterr().err
