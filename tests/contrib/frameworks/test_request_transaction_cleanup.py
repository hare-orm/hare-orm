"""All request connections finish cleanup before errors or response headers escape."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hare.contrib.frameworks.request_transaction import RequestTransaction
from hare.transactions import Transactions

pytestmark = [pytest.mark.database_independent, pytest.mark.asyncio]


@pytest.mark.parametrize("failure_type", [RuntimeError, asyncio.CancelledError])
async def test_exit_failure_rolls_back_remaining_connections(monkeypatch, failure_type):
    failure = failure_type("exit failed")
    contexts = {
        alias: SimpleNamespace(__aenter__=AsyncMock(), __aexit__=AsyncMock()) for alias in ("first", "second", "third")
    }
    contexts["second"].__aexit__.side_effect = failure
    monkeypatch.setattr(Transactions, "atomic", lambda alias: contexts[alias])
    request = RequestTransaction(tuple(contexts))
    await request.begin()
    with pytest.raises(failure_type) as raised:
        await request.finish(200)
    assert raised.value is failure
    assert [result.connection_alias for result in request.exits] == ["third", "second", "first"]
    assert [result.commit_requested for result in request.exits] == [True, True, False]
    assert contexts["first"].__aexit__.await_args.args[1] is failure
    await request.close(failure)
    assert all(context.__aexit__.await_count == 1 for context in contexts.values())


async def test_request_and_all_cleanup_failures_are_preserved(monkeypatch):
    request_error = LookupError("request failed")
    failures = [RuntimeError("first exit"), RuntimeError("second exit")]
    contexts = {
        alias: SimpleNamespace(__aenter__=AsyncMock(), __aexit__=AsyncMock(side_effect=failure))
        for alias, failure in zip(("first", "second"), failures, strict=True)
    }
    monkeypatch.setattr(Transactions, "atomic", lambda alias: contexts[alias])
    request = RequestTransaction(tuple(contexts))
    await request.begin()
    with pytest.raises(ExceptionGroup) as raised:
        await request.close(request_error)
    assert raised.value.exceptions == (request_error, failures[1], failures[0])
    assert all(not result.commit_requested for result in request.exits)
    assert all(context.__aexit__.await_args.args[1] is request_error for context in contexts.values())


async def test_failed_second_entry_closes_first_connection(monkeypatch):
    failure = RuntimeError("cannot enter")
    first = SimpleNamespace(__aenter__=AsyncMock(), __aexit__=AsyncMock())
    second = SimpleNamespace(__aenter__=AsyncMock(side_effect=failure), __aexit__=AsyncMock())
    monkeypatch.setattr(Transactions, "atomic", {"first": first, "second": second}.__getitem__)
    request = RequestTransaction(("first", "second"))
    with pytest.raises(RuntimeError) as raised:
        await request.begin()
    assert raised.value is failure
    assert first.__aexit__.await_args.args[1] is failure
    second.__aexit__.assert_not_awaited()


async def test_repeated_cancellation_drains_active_exit_then_rolls_back_rest(monkeypatch):
    entered, release, notified = asyncio.Event(), asyncio.Event(), asyncio.Event()
    first = SimpleNamespace(__aenter__=AsyncMock(), __aexit__=AsyncMock())

    async def finish_second(*args):
        entered.set()
        await release.wait()

    second = SimpleNamespace(__aenter__=AsyncMock(), __aexit__=AsyncMock(side_effect=finish_second))
    monkeypatch.setattr(Transactions, "atomic", {"first": first, "second": second}.__getitem__)
    request = RequestTransaction(("first", "second"))
    cancel_close = request._cancel_close

    def notify_cancel(error):
        cancel_close(error)
        notified.set()

    monkeypatch.setattr(request, "_cancel_close", notify_cancel)
    await request.begin()
    waiter = asyncio.create_task(request.finish(200))
    try:
        await entered.wait()
        for _ in range(2):
            notified.clear()
            waiter.cancel()
            await notified.wait()
            assert not waiter.done()
            first.__aexit__.assert_not_awaited()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await waiter
    assert isinstance(first.__aexit__.await_args.args[1], asyncio.CancelledError)
    assert [result.commit_requested for result in request.exits] == [True, False]
    assert request.close_task.done()


async def test_asgi_headers_follow_commit_and_body_error_does_not_close_twice(monkeypatch):
    events = []
    failure = RuntimeError("body failed")

    async def close(*args):
        events.append("commit")

    context = SimpleNamespace(__aenter__=AsyncMock(), __aexit__=AsyncMock(side_effect=close))
    monkeypatch.setattr(Transactions, "atomic", lambda alias: context)

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200})
        events.append("body")
        raise failure

    async def send(message):
        events.append("headers")

    with pytest.raises(RuntimeError) as raised:
        await RequestTransaction((None,)).run_asgi(app, {}, AsyncMock(), send)
    assert raised.value is failure
    assert events == ["commit", "headers", "body"]
    context.__aexit__.assert_awaited_once_with(None, None, None)


async def test_asgi_commit_error_prevents_headers(monkeypatch):
    failure = RuntimeError("commit failed")
    context = SimpleNamespace(__aenter__=AsyncMock(), __aexit__=AsyncMock(side_effect=failure))
    monkeypatch.setattr(Transactions, "atomic", lambda alias: context)
    send = AsyncMock()

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200})

    with pytest.raises(RuntimeError) as raised:
        await RequestTransaction((None,)).run_asgi(app, {}, AsyncMock(), send)
    assert raised.value is failure
    send.assert_not_awaited()
