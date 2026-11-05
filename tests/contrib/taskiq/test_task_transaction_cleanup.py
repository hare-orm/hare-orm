"""Task middleware restores scopes only after every transaction has exited."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hare.contrib.taskiq.hare_taskiq_middleware import HareTaskiqMiddleware
from hare.instrumentation.queries.query_tags import QueryTags
from hare.transactions.transaction_group import TransactionGroup
from hare.transactions.transactions import Transactions

pytestmark = [pytest.mark.database_independent, pytest.mark.asyncio]


@pytest.mark.parametrize("failure_type", [LookupError, asyncio.CancelledError])
async def test_partial_task_start_rolls_back_and_restores_scopes(monkeypatch, failure_type):
    failure = failure_type("cannot open second connection")
    first = SimpleNamespace(__aenter__=AsyncMock(), __aexit__=AsyncMock())
    second = SimpleNamespace(__aenter__=AsyncMock(side_effect=failure), __aexit__=AsyncMock())
    monkeypatch.setattr(Transactions, "atomic", {"first": first, "second": second}.__getitem__)
    middleware = HareTaskiqMiddleware(atomic_tasks=("first", "second"))
    message = SimpleNamespace(task_id="job", task_name="read", labels={})
    previous_tags = QueryTags.current.get()
    previous_task = middleware.running_task.get()
    with pytest.raises(failure_type) as caught:
        await middleware.pre_execute(message)
    assert caught.value is failure
    first.__aexit__.assert_awaited_once()
    assert first.__aexit__.await_args.args[:2] == (failure_type, failure)
    second.__aexit__.assert_not_called()
    assert QueryTags.current.get() is previous_tags
    assert middleware.running_task.get() is previous_task


async def test_task_completion_cancellation_drains_exits_before_restoring_scopes(monkeypatch):
    entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    exits = []
    original_cancel = TransactionGroup._cancel_close

    def record_cancel(group, error):
        original_cancel(group, error)
        cancelled.set()

    async def exit_first(exception_type, error, traceback):
        exits.append(("first", exception_type))

    async def exit_second(exception_type, error, traceback):
        entered.set()
        await release.wait()
        exits.append(("second", exception_type))

    contexts = {
        "first": SimpleNamespace(__aenter__=AsyncMock(), __aexit__=exit_first),
        "second": SimpleNamespace(__aenter__=AsyncMock(), __aexit__=exit_second),
    }
    monkeypatch.setattr(Transactions, "atomic", contexts.__getitem__)
    monkeypatch.setattr(TransactionGroup, "_cancel_close", record_cancel)
    middleware = HareTaskiqMiddleware(atomic_tasks=("first", "second"))
    message = SimpleNamespace(task_id="job", task_name="read", labels={})
    previous_tags = QueryTags.current.get()
    previous_task = middleware.running_task.get()

    async def work():
        await middleware.pre_execute(message)
        try:
            await middleware.post_execute(message, SimpleNamespace(is_err=False, error=None))
        finally:
            assert QueryTags.current.get() is previous_tags
            assert middleware.running_task.get() is previous_task

    task = asyncio.create_task(work())
    try:
        await entered.wait()
        task.cancel()
        await cancelled.wait()
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert exits == [("second", None), ("first", asyncio.CancelledError)]
