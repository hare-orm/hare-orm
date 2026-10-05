"""The completion queue of an event loop the rust_pg driver resolved queries on is collected with the
loop - with its wake socket - instead of staying alive for good."""

import asyncio
import contextvars
import gc
import threading
import time
import weakref
from typing import Any

import pytest

from hare import Connections
from hare.contrib.test import requires_features
from hare.dialects.postgresql.drivers.rust_pg.client import RustPgClient
from hare.native.native_modules import NativeModules
from tests.utils.waiting import wait_until


def get_completion_queues() -> list[Any]:
    # A reference a tokio worker let go of without the GIL is released by pyo3 on the next call into the
    # extension - made here, so a queue nothing holds any more is gone before the objects are listed.
    NativeModules.clock.get_posix_time()
    gc.collect()
    return [item for item in gc.get_objects() if type(item).__name__ == "CompletionQueue"]


def get_queue_loop(queue: Any) -> Any:
    """The event loop of a queue - None once the loop is collected (the queue keeps a weak reference)."""
    weak_loops = [referent for referent in gc.get_referents(queue) if isinstance(referent, weakref.ref)]
    assert len(weak_loops) == 1
    return weak_loops[0]()


async def query_on_a_new_client(weak_loops: list[weakref.ref[asyncio.AbstractEventLoop]]) -> None:
    weak_loops.append(weakref.ref(asyncio.get_running_loop()))
    client = Connections.current().create_independent("models", {"min_size": 1, "max_size": 1})
    try:
        assert await client.execute_dicts("SELECT 1 AS one") == [{"one": 1}]
        running_loop = asyncio.get_running_loop()
        assert any(get_queue_loop(queue) is running_loop for queue in get_completion_queues())
    finally:
        await client.close()


def run_loops(
    context: contextvars.Context, count: int, weak_loops: list[weakref.ref[asyncio.AbstractEventLoop]]
) -> None:
    for _ in range(count):
        context.run(asyncio.run, query_on_a_new_client(weak_loops))


async def start_a_slow_query(weak_loops: list[weakref.ref[asyncio.AbstractEventLoop]]) -> tuple[Any, Any]:
    weak_loops.append(weakref.ref(asyncio.get_running_loop()))
    client = Connections.current().create_independent("models", {"min_size": 1, "max_size": 1})
    assert await client.execute_dicts("SELECT 1 AS one") == [{"one": 1}]
    # The pool's own future, not awaited - its result comes after the loop closed.
    return client, client._pool.fetch_all("SELECT pg_sleep(0.3)", [])


def close_the_loop_before_a_result(
    context: contextvars.Context, weak_loops: list[weakref.ref[asyncio.AbstractEventLoop]]
) -> None:
    event_loop = asyncio.new_event_loop()
    try:
        client, pending_result = context.run(event_loop.run_until_complete, start_a_slow_query(weak_loops))
    finally:
        event_loop.close()
    del client, pending_result
    # The query ends meanwhile and its result goes to the queue of the closed loop.
    time.sleep(1)


def get_kept_queues(queue_ids_before: set[int], weak_loops: list[weakref.ref[asyncio.AbstractEventLoop]]) -> list[Any]:
    """The queues kept past their loops - of these loops, or of loops collected already. A queue of
    another test's loop still running counts for nothing."""
    kept_queues = []
    for queue in get_completion_queues():
        if id(queue) in queue_ids_before:
            continue
        event_loop = get_queue_loop(queue)
        if event_loop is None or any(event_loop is weak_loop() for weak_loop in weak_loops):
            kept_queues.append(queue)
    return kept_queues


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "run_loops_in_thread",
    [
        lambda context, weak_loops: run_loops(context, 5, weak_loops),
        close_the_loop_before_a_result,
    ],
    ids=["loops_run_to_their_end", "a_result_comes_after_its_loop_closed"],
)
async def test_the_queue_of_a_closed_event_loop_is_collected(db_simple, run_loops_in_thread):
    """A result coming after its loop closed stayed in the queue for good - its future kept the loop,
    the loop the queue."""
    if not isinstance(Connections.get("models"), RustPgClient):
        pytest.skip("the completion queue is the rust_pg driver's")
    queue_ids_before = {id(queue) for queue in get_completion_queues()}
    weak_loops: list[weakref.ref[asyncio.AbstractEventLoop]] = []
    thread = threading.Thread(target=run_loops_in_thread, args=(contextvars.copy_context(), weak_loops))
    thread.start()
    thread.join(timeout=60)
    assert not thread.is_alive()
    assert weak_loops
    # The driver's threads let go of a closed loop's queue on their own time - a leaked one never goes.
    await wait_until(lambda: not get_kept_queues(queue_ids_before, weak_loops))
