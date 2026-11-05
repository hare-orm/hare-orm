import asyncio
from unittest.mock import AsyncMock

import pytest

from hare.core.hare_context import HareContext
from hare.instrumentation.declarations import QueryExecuted
from hare.instrumentation.observers.observers import Observers

pytestmark = [pytest.mark.database_independent, pytest.mark.asyncio]


@pytest.mark.parametrize("cancelled_result", ["future", "coroutine"])
async def test_cancelled_callback_keeps_sibling_cleanup_in_dispatch(cancelled_result):
    context = HareContext()
    started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    finished = []

    def cancelled(event):
        if cancelled_result == "future":
            future = asyncio.get_running_loop().create_future()
            future.cancel()
            return future

        async def callback():
            raise asyncio.CancelledError

        return callback()

    async def sibling(event):
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            finished.append(event.sql)

    context.observers.add(QueryExecuted, cancelled, None)
    context.observers.add(QueryExecuted, sibling, None)
    try:
        with context:
            Observers.notify(QueryExecuted("query", None, 0, None, "default"))
            async with asyncio.timeout(5):
                await started.wait()
                async with asyncio.TaskGroup() as tasks:
                    drain = tasks.create_task(Observers.wait_for_pending(timeout_seconds=0))
                    await cleaning.wait()
                    assert not drain.done()
                    release.set()
            assert finished == ["query"]
            assert not Observers.get_dispatcher().pending_tasks
    finally:
        release.set()
        await context.close_connections()
        context.observers.clear()


async def test_context_queues_and_errors_are_independent_and_callbacks_run_once():
    first, second = HareContext(), HareContext()
    release = asyncio.Event()
    started = asyncio.Event()
    calls = []

    async def slow(event):
        started.set()
        await release.wait()
        assert HareContext.get_current() is first
        raise ValueError("first observer")

    def returns_awaitable(event):
        calls.append(("called", event.sql))

        async def delivered():
            assert HareContext.get_current() is second
            calls.append(("awaited", event.sql))

        return delivered()

    first.observers.add(QueryExecuted, slow, None)
    second.observers.add(QueryExecuted, returns_awaitable, None)
    try:
        with first:
            Observers.notify(QueryExecuted("first", None, 0, None, "default"))
            first_dispatcher = Observers.get_dispatcher()
        await started.wait()
        with second:
            Observers.notify(QueryExecuted("second", None, 0, None, "default"))
            await Observers.wait_for_pending()
        assert first_dispatcher.pending_tasks
        assert calls == [("called", "second"), ("awaited", "second")]
        release.set()
        with first:
            with pytest.raises(ValueError, match="first observer"):
                await Observers.wait_for_pending()
        with second:
            await Observers.wait_for_pending()
    finally:
        release.set()
        await first.close_connections()
        await second.close_connections()
        first.observers.clear()
        second.observers.clear()


async def test_context_drains_observers_before_closing_its_pools(monkeypatch):
    context = HareContext()
    delivered = []

    async def observer(event):
        delivered.append(event.sql)

    context.observers.add(QueryExecuted, observer, None)
    try:
        with context:
            Observers.notify(QueryExecuted("query", None, 0, None, "default"))
            handler = context.connections
            handler._db_config = {}

        async def close_all(**kwargs):
            assert delivered == ["query"]

        close = AsyncMock(side_effect=close_all)
        monkeypatch.setattr(handler, "close_all", close)
        await context.close_connections()
        close.assert_awaited_once_with(discard=True)
    finally:
        await context.close_connections()
        context.observers.clear()


async def test_cancelled_direct_shutdown_finishes_observers_before_releasing_pools(monkeypatch):
    context = HareContext()
    draining, release = asyncio.Event(), asyncio.Event()
    order = []

    async def observer(event):
        await release.wait()
        order.append("observer")

    context.observers.add(QueryExecuted, observer, None)
    original_drain = context.observers.wait_for_pending

    async def drain(**kwargs):
        draining.set()
        await original_drain(**kwargs)

    async def close_all(**kwargs):
        order.append("pools")

    with context:
        Observers.notify(QueryExecuted("query", None, 0, None, "default"))
        handler = context.connections
        handler._db_config = {}
    monkeypatch.setattr(context.observers, "wait_for_pending", drain)
    monkeypatch.setattr(handler, "close_all", close_all)
    task = asyncio.create_task(context.close_connections())
    try:
        await draining.wait()
        task.cancel()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        context.observers.clear()
    assert order == ["observer", "pools"]
    assert context._connections is None
    assert not context._inited
