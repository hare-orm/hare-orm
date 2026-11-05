import asyncio
from types import SimpleNamespace

import pytest

from hare.dialects.base.client.connection_wrapper import ConnectionWrapper
from hare.dialects.base.transactions.savepoints.nested_savepoint_lock import NestedSavepointLock

pytestmark = pytest.mark.database_independent


@pytest.mark.asyncio
async def test_cancellation_waiting_for_physical_lock_releases_only_the_query_span():
    lock = asyncio.Lock()
    spans = NestedSavepointLock()
    connection = object()
    client = SimpleNamespace(
        is_transaction_client=True,
        _savepoint_lock=spans,
        _connection=connection,
        _check_statement_allowed=lambda: None,
        _outer_transaction=None,
        _begin_pending=False,
        _pending_savepoints=[],
    )
    wrapper = ConnectionWrapper(lock, client)
    await lock.acquire()
    task = asyncio.create_task(wrapper.__aenter__())
    try:
        async with asyncio.timeout(10):
            while wrapper._span is None:
                await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert lock.locked(), "The cancelled waiter must not release another operation's lock"
        assert not spans._open_spans
        lock.release()
        async with asyncio.timeout(10), ConnectionWrapper(lock, client) as acquired:
            assert acquired is connection
        assert not lock.locked()
        assert not spans._open_spans
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if lock.locked():
            lock.release()
