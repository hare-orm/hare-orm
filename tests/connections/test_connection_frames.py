from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from hare.core.connections.connection_handler import ConnectionHandler
from hare.exceptions import ConfigurationError

pytestmark = pytest.mark.database_independent


def test_main_client_is_separate_from_nested_transaction_bindings():
    handler = ConnectionHandler()
    root = SimpleNamespace(_check_loop=lambda: True, tenant_schema_template=None, tenant_row_level_security=False)
    first = SimpleNamespace(tenant_schema_template=None, tenant_row_level_security=False)
    second = SimpleNamespace(tenant_schema_template=None, tenant_row_level_security=False)
    handler.root_clients["default"] = root
    outer = handler.set("default", first)
    inner = handler.set("default", second)
    assert handler.get_main("default") is root
    assert handler.get("default") is second
    inner.reset()
    assert handler.get("default") is first
    outer.reset()
    assert handler.get("default") is root
    with pytest.raises(ValueError):
        outer.reset()


@pytest.mark.asyncio
async def test_child_release_is_visible_in_parent_without_changing_its_frame():
    handler = ConnectionHandler()
    root = SimpleNamespace(_check_loop=lambda: True, tenant_schema_template=None, tenant_row_level_security=False)
    transaction = SimpleNamespace(tenant_schema_template=None, tenant_row_level_security=False)
    handler.root_clients["default"] = root
    token = handler.set("default", transaction)
    frame = handler.get_frame()

    async def finish():
        assert handler.get("default") is transaction
        token.reset()
        assert handler.get("default") is root

    await asyncio.create_task(finish())
    assert handler.get_frame() is frame
    assert handler.get("default") is root


def test_releasing_one_alias_preserves_another_alias_binding():
    handler = ConnectionHandler()
    first, second = object(), object()
    first_token = handler.set("first", first)
    second_token = handler.set("second", second)
    first_token.reset()
    assert handler.get_frame().get("first") is None
    assert handler.get_frame().get("second") is second
    second_token.reset()


@pytest.mark.asyncio
async def test_cancelled_shutdown_drains_once_and_blocks_new_acquisition():
    handler = ConnectionHandler()
    started, release = asyncio.Event(), asyncio.Event()
    closed = []

    async def close():
        started.set()
        await release.wait()
        closed.append(True)

    root = SimpleNamespace(close=close, discarded=False)
    handler.root_clients["default"] = root
    task = asyncio.create_task(handler.close_all(discard=False))
    await started.wait()
    also_closing = asyncio.create_task(handler.close_all(discard=True))
    task.cancel()
    with pytest.raises(ConfigurationError, match="closing"):
        handler.get_main("default")
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await also_closing
    assert closed == [True]
    assert root.discarded
    assert not handler.root_clients


@pytest.mark.asyncio
async def test_shutdown_does_not_close_another_handlers_pending_resources():
    first, second = ConnectionHandler(), ConnectionHandler()
    release = asyncio.Event()
    second._schedule_stale_connection_close(SimpleNamespace(close=release.wait, _bound_loop=None))
    pending = tuple(second.stale_connection_closes)
    await first.close_all()
    assert pending and all(not task.done() for task in pending)
    release.set()
    await second.close_all()
    assert not second.stale_connection_closes
