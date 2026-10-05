"""OutboxRelay resolves its connection through the router-aware Model.get_connection() that
OutboxEvent.enqueue() uses, not the model's static Meta.default_connection - a router sending the
model to another alias otherwise made the relay claim, lock and listen on the wrong database, and it
never delivered anything.

Uses two real, independently created databases (two real sqlite files - or, when HARE_TEST_DB
points at Postgres, two real Postgres databases, following the exact convention
test_get_or_create_router.py/test_outbox_cross_connection.py already use) so the router-chosen
alias is a genuinely different physical database from Meta.default_connection, not just a second
in-memory handle.
"""

from __future__ import annotations

import os
import sys
import types
from typing import Any
from unittest import mock

import pytest
import pytest_asyncio

from hare.contrib.outbox import OutboxRelay, OutboxWakeup
from hare.contrib.outbox.outbox_event import OutboxEvent
from hare.contrib.test import requires_features
from hare.dialects.sqlite.clauses.sqlite_query_clauses import SqliteQueryClauses
from tests.utils.multi_database_context import MultiDatabaseTestContext

MODULE_NAME = "tests._relay_router_outbox_models"


class OutboxAliasRouter:
    """Routes RoutedOutboxEvent's reads and writes to a connection OTHER than its own
    Meta.default_connection - the same shape a real project's router takes when a shared outbox
    table physically lives on a different database than its own app's default connection."""

    def db_for_read(self, model: type) -> str | None:
        if model.__name__ == "RoutedOutboxEvent":
            return "outbox_router_target"
        return None

    def db_for_write(self, model: type) -> str | None:
        return self.db_for_read(model)


@pytest_asyncio.fixture
async def routed_outbox(tmp_path):
    class RoutedOutboxEvent(OutboxEvent):
        class Meta(OutboxEvent.Meta):
            app = "relay_router_outbox"

    module = types.ModuleType(MODULE_NAME)
    setattr(module, "RoutedOutboxEvent", RoutedOutboxEvent)  # noqa: B010
    sys.modules[MODULE_NAME] = module

    db_url = os.getenv("HARE_TEST_DB") or f"sqlite+aiosqlite:///{tmp_path}/outbox_{{}}.sqlite"
    try:
        async with MultiDatabaseTestContext.open(
            db_url,
            ["outbox_default", "outbox_router_target"],
            apps={"relay_router_outbox": {"models": [MODULE_NAME], "default_connection": "outbox_default"}},
            routers=[OutboxAliasRouter],
        ) as ctx:
            await ctx.generate_schemas()
            # Schema generation only ever creates tables for a model's static default connection
            # ("outbox_default" here) - "outbox_router_target" needs the same DDL applied
            # manually, same as a real router setup assumes is already in place there.
            default_db = ctx.connections.get("outbox_default")
            router_target_db = ctx.connections.get("outbox_router_target")
            await router_target_db.execute_script(default_db.get_schema_sql(safe=True))
            yield ctx, RoutedOutboxEvent
    finally:
        sys.modules.pop(MODULE_NAME, None)


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_the_claim_resolves_the_router_alias_enqueue_used(routed_outbox):
    """support_for_update is forced True on both connections (real sqlite never reports it) to
    force this code down the SELECT ... FOR UPDATE claim path deterministically even off
    Postgres; the FOR UPDATE clause itself is stripped back out of the generated SQL (sqlite has
    no such syntax to execute) - only the connection/transaction resolution this bug was actually
    about is under test here, not real row locking (see test_relay_postgres.py's
    test_concurrent_relays_never_deliver_an_event_twice for that, which needs a real Postgres
    server to mean anything)."""
    ctx, RoutedOutboxEvent = routed_outbox
    default_db = ctx.connections.get("outbox_default")
    router_target_db = ctx.connections.get("outbox_router_target")

    event = await RoutedOutboxEvent.enqueue("widget.updated", {"i": 1})

    # enqueue() already resolves through the router - confirms this test's own setup actually
    # sends this model to a DIFFERENT alias than Meta.default_connection, not just repeats it.
    assert await RoutedOutboxEvent.objects.all().using(default_db).count() == 0
    assert await RoutedOutboxEvent.objects.all().using(router_target_db).count() == 1

    delivered_ids: list[Any] = []

    async def deliver(delivered_event: Any) -> None:
        delivered_ids.append(delivered_event.id)

    relay = OutboxRelay(RoutedOutboxEvent, deliver, batch_size=5)

    with (
        mock.patch.object(default_db, "features", default_db.features.replace(supports_select_for_update=True)),
        mock.patch.object(
            router_target_db, "features", router_target_db.features.replace(supports_select_for_update=True)
        ),
        mock.patch.object(SqliteQueryClauses, "get_row_lock_sql", lambda self, builder, ctx: ""),
    ):
        claimed = await relay.poll_once()

    assert claimed == 1
    assert delivered_ids == [event.id]

    refreshed = await RoutedOutboxEvent.objects.using(router_target_db).get(id=event.id)
    assert refreshed.published_at is not None


@pytest.mark.asyncio
async def test_the_wakeup_subscription_uses_the_router_resolved_connection(routed_outbox):
    """The relay subscribes its wakeup on the connection enqueue() writes through - the
    router-chosen one, not the model's static default connection."""
    _ctx, RoutedOutboxEvent = routed_outbox
    subscribed: list[str] = []

    class RecordingWakeup(OutboxWakeup):
        async def subscribe(self, callback: Any, connection_alias: str) -> None:
            subscribed.append(connection_alias)

        async def unsubscribe(self, callback: Any) -> None:
            pass

    async def deliver(event: Any) -> None:
        pass

    async with OutboxRelay(RoutedOutboxEvent, deliver, wakeup=RecordingWakeup()):
        pass

    assert subscribed == ["outbox_router_target"]
