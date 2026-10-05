"""The completion queue of an event loop the rust_pg driver resolved queries on is collected with the
loop - with its wake socket - instead of staying alive for good."""

import asyncio
import contextvars
import gc
import threading

import pytest

from hare import Connections
from hare.contrib.test import requires_features
from hare.dialects.postgresql.drivers.rust_pg.client import RustPgClient


def count_completion_queues() -> int:
    gc.collect()
    return sum(1 for item in gc.get_objects() if type(item).__name__ == "CompletionQueue")


async def query_on_a_new_client() -> None:
    client = Connections.current().create_independent("models", {"min_size": 1, "max_size": 1})
    try:
        assert await client.execute_dicts("SELECT 1 AS one") == [{"one": 1}]
    finally:
        await client.close()


def run_loops(context: contextvars.Context, count: int) -> None:
    for _ in range(count):
        context.run(asyncio.run, query_on_a_new_client())


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_the_queue_of_a_closed_event_loop_is_collected(db_simple):
    if not isinstance(Connections.get("models"), RustPgClient):
        pytest.skip("the completion queue is the rust_pg driver's")
    before = count_completion_queues()
    thread = threading.Thread(target=run_loops, args=(contextvars.copy_context(), 5))
    thread.start()
    thread.join(timeout=60)
    assert not thread.is_alive()
    assert count_completion_queues() <= before
