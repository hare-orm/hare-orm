import asyncio
from types import SimpleNamespace

import pytest

from hare.core.connections.connection_handler import ConnectionHandler
from hare.dialects.base.transactions.contexts.nested_transaction_context import NestedTransactionContext
from hare.dialects.base.transactions.savepoints.nested_savepoint_lock import NestedSavepointLock
from hare.dialects.base.transactions.savepoints.savepoint_span import current_savepoint_span

pytestmark = pytest.mark.database_independent


@pytest.mark.asyncio
async def test_abandoned_savepoint_span_release_is_kept_until_it_releases_the_span():
    """A nested transaction whose RELEASE/ROLLBACK TO hit the shielded wait's bound releases its span
    from a background task once that operation lands. The event loop keeps a task only weakly - the
    task was referenced by nothing, so the garbage collector could drop it and leave the span held
    forever, wedging every later sibling on the connection."""
    lock = NestedSavepointLock()
    span = await lock.acquire(None)
    still_running = asyncio.get_running_loop().create_future()
    client = SimpleNamespace(
        connection_alias="default", _finalized=True, _shielded_savepoint_abandoned_task=still_running
    )
    context = NestedTransactionContext(client, lock)
    context._span = span
    context._span_reset_token = current_savepoint_span.set(span)

    await context.__aexit__(None, None, None)

    span_releases = set(NestedTransactionContext.span_releases)
    assert len(span_releases) == 1
    still_running.set_result(None)
    await asyncio.gather(*span_releases)
    assert not span_releases & NestedTransactionContext.span_releases
    # The span is released - the next sibling takes one at once.
    await asyncio.wait_for(lock.acquire(None), timeout=1)


@pytest.mark.asyncio
async def test_stale_connection_close_is_kept_until_it_ends():
    """A connection replaced after an event loop change is closed by a task nothing referenced -
    one the garbage collector could drop before the connection was closed."""
    may_close = asyncio.Event()
    closed = []

    async def close() -> None:
        await may_close.wait()
        closed.append(True)

    ConnectionHandler()._schedule_stale_connection_close(SimpleNamespace(close=close))

    closes = set(ConnectionHandler.stale_connection_closes)
    assert len(closes) == 1
    may_close.set()
    await asyncio.gather(*closes)
    assert closed == [True]
    assert not closes & ConnectionHandler.stale_connection_closes
