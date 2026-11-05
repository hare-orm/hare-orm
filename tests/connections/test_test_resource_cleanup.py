from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from hare.contrib.test.databases.resource_cleanup import ResourceCleanup
from hare.contrib.test.databases.rollback_isolation import RollbackIsolation
from hare.contrib.test.databases.temporary_databases import TemporaryDatabases
from hare.core.hare_context import HareContext
from hare.core.resource_completion import ResourceCompletion
from hare.exceptions import ConfigurationError


@pytest.mark.asyncio
async def test_cancelled_waiter_retains_the_release_failure_without_background_errors():
    started, finish = asyncio.Event(), asyncio.Event()
    failure = RuntimeError("release failed")
    loop = asyncio.get_running_loop()
    reported = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: reported.append(context))

    async def release():
        started.set()
        await finish.wait()
        raise failure

    try:
        waiter = asyncio.create_task(ResourceCompletion.wait(release()))
        await started.wait()
        waiter.cancel()
        finish.set()
        with pytest.raises(BaseExceptionGroup) as captured:
            await waiter
        assert failure in captured.value.exceptions
        assert any(isinstance(error, asyncio.CancelledError) for error in captured.value.exceptions)
        assert reported == []
    finally:
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
async def test_repeated_cancellation_drains_every_release_and_preserves_errors():
    entered, finish = asyncio.Event(), asyncio.Event()
    released = []
    cleanup = ResourceCleanup()
    failure = RuntimeError("release failed")

    async def release_first():
        released.append("first")

    async def release_last():
        entered.set()
        await finish.wait()
        released.append("last")
        raise failure

    cleanup.push(release_first)
    cleanup.push(release_last)
    waiter = asyncio.create_task(cleanup.close())
    await entered.wait()
    waiter.cancel()
    acknowledged = asyncio.Event()
    asyncio.get_running_loop().call_soon(acknowledged.set)
    await acknowledged.wait()
    waiter.cancel()
    finish.set()
    await waiter
    assert released == ["last", "first"]
    with pytest.raises(BaseExceptionGroup) as captured:
        cleanup.raise_errors()
    assert failure in captured.value.exceptions
    assert any(isinstance(error, asyncio.CancelledError) for error in captured.value.exceptions)
    await cleanup.close()
    assert released == ["last", "first"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("rollback failed"), asyncio.CancelledError()])
async def test_isolation_releases_all_transactions_and_restores_context(failure):
    transactions = [MagicMock(), MagicMock(), MagicMock()]
    released = []

    def connection(index):
        transaction = transactions[index]
        transaction.__aenter__ = AsyncMock(return_value=SimpleNamespace())

        async def exit_transaction(*args):
            released.append(index)
            if index == 1:
                raise failure

        transaction.__aexit__ = exit_transaction
        return SimpleNamespace(
            connection_alias=str(index),
            features=SimpleNamespace(supports_transactions=True),
            _in_transaction=lambda: transaction,
        )

    context = HareContext()
    clients = [connection(index) for index in range(3)]
    context._connections = SimpleNamespace(all=lambda: clients)
    original = ValueError("body failed")
    with HareContext() as previous:
        isolation = RollbackIsolation(context)
        with pytest.raises(BaseExceptionGroup) as captured:
            async with isolation:
                raise original
        assert HareContext.require_current() is previous
    assert released == [2, 1, 0]
    assert captured.value.exceptions == (original, failure)


@pytest.fixture
def temporary_databases():
    temporary = TemporaryDatabases(
        {"connections": {"first": {}, "second": {}, "third": {}}, "apps": {}},
        "sqlite+aiosqlite://:memory:",
        reuse_databases=False,
    )
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=context)
    context.__aexit__ = AsyncMock()
    context.init = AsyncMock()
    context.generate_schemas = AsyncMock()
    context.connections.close_all = AsyncMock()
    clients = {
        name: SimpleNamespace(
            connection_alias=name,
            db_create=AsyncMock(),
            db_delete=AsyncMock(),
            close=AsyncMock(),
            generate_schema=AsyncMock(),
        )
        for name in temporary.config["connections"]
    }
    context.connections.get_main.side_effect = clients.__getitem__
    temporary.context = context
    return temporary, clients


@pytest.mark.asyncio
async def test_partial_database_creation_releases_only_acquired_resources(temporary_databases):
    temporary, clients = temporary_databases
    original = RuntimeError("creation failed")
    release_error = ValueError("release failed")
    clients["second"].db_create.side_effect = original
    clients["first"].db_delete.side_effect = release_error
    with pytest.raises(ExceptionGroup) as captured:
        async with temporary:
            pytest.fail("startup must fail")
    assert captured.value.exceptions == (original, release_error)
    clients["first"].db_delete.assert_awaited_once()
    clients["second"].db_delete.assert_not_awaited()
    clients["third"].db_create.assert_not_awaited()
    clients["third"].db_delete.assert_not_awaited()
    temporary.context.__aexit__.assert_awaited_once()


@pytest.mark.asyncio
async def test_pool_close_failure_does_not_skip_database_releases(temporary_databases):
    temporary, clients = temporary_databases
    failure = RuntimeError("pool close failed")
    temporary.context.connections.close_all.side_effect = failure
    with pytest.raises(RuntimeError) as captured:
        async with temporary:
            pass
    assert captured.value is failure
    for client in clients.values():
        client.db_delete.assert_awaited_once()
    temporary.context.__aexit__.assert_awaited_once()


@pytest.mark.asyncio
async def test_nested_reentry_does_not_close_the_active_temporary_scope(temporary_databases):
    temporary, clients = temporary_databases
    async with temporary:
        with pytest.raises(ConfigurationError, match="already active"):
            await temporary.__aenter__()
        for client in clients.values():
            client.db_delete.assert_not_awaited()
    for client in clients.values():
        client.db_delete.assert_awaited_once()
    await temporary.__aexit__(None, None, None)
    temporary.context.__aexit__.assert_awaited_once()
