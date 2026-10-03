"""Regression coverage for OutboxRelay resolving its own connection through the model's static
Meta.db/Meta.default_connection instead of the router-aware Model.get_connection() that
OutboxEvent.publish() itself already uses (see OutboxEvent.publish()'s own docstring).

A ConnectionRouter sending this model's traffic to a DIFFERENT alias than its own
Meta.default_connection used to make _poll_once()/_claim_and_deliver_one()/_run_listen_loop()
read/lock/listen on the WRONG (static) alias while publish() itself (and this relay's own
_base_queryset()) correctly resolved onto the router-chosen alias instead - confirmed live: on a
backend with SELECT ... FOR UPDATE support (real Postgres), _claim_and_deliver_one() opened its
Transactions.atomic() block on the static alias while its own _base_queryset() query
landed on the router-chosen alias, which had no active transaction at all - select_for_update()
raised ConfigurationError every single cycle, and the relay never claimed a single row (that
failure is caught and logged by _poll_once_safely(), so from the app's perspective the relay just
silently never delivered anything).

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

from hare.contrib.notify import NotificationListener
from hare.contrib.outbox import OutboxRelay
from hare.contrib.outbox.constants import LISTEN_MAX_RECONNECT_ATTEMPTS
from hare.contrib.outbox.models import OutboxEvent
from hare.contrib.test import requires_features
from hare.sql.queries import QueryBuilder
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

    db_url = os.getenv("HARE_TEST_DB") or f"sqlite:///{tmp_path}/outbox_{{}}.sqlite"
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
async def test_claim_and_deliver_one_resolves_the_same_router_alias_publish_used(routed_outbox):
    """support_for_update is forced True on both connections (real sqlite never reports it) to
    force this code down the SELECT ... FOR UPDATE claim path deterministically even off
    Postgres; the FOR UPDATE clause itself is stripped back out of the generated SQL (sqlite has
    no such syntax to execute) - only the connection/transaction resolution this bug was actually
    about is under test here, not real row locking (see test_relay_postgres.py's
    test_skip_locked_never_double_claims_the_row_a_concurrent_relay_is_delivering for that, which
    needs a real Postgres server to mean anything)."""
    ctx, RoutedOutboxEvent = routed_outbox
    default_db = ctx.connections.get("outbox_default")
    router_target_db = ctx.connections.get("outbox_router_target")

    event = await RoutedOutboxEvent.publish(topic="widget.updated", payload={"i": 1})

    # publish() already resolves through the router - confirms this test's own setup actually
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
        mock.patch.object(QueryBuilder, "_for_update_sql", lambda self, ctx, lock_strength="UPDATE": ""),
    ):
        claimed = await relay._poll_once()

    assert claimed == 1
    assert delivered_ids == [event.id]

    refreshed = await RoutedOutboxEvent.objects.using(router_target_db).get(id=event.id)
    assert refreshed.published_at is not None


@pytest.mark.asyncio
async def test_run_listen_loop_uses_the_router_resolved_connection(routed_outbox):
    """_run_listen_loop() used to check `self.model._meta.db` (the static default connection) for
    LISTEN/NOTIFY support and to LISTEN on it, instead of the router-resolved connection
    publish() actually writes to - a NOTIFY sent from the real (router-chosen) write connection
    was never seen by a LISTEN opened on the wrong one.

    Reproduced here without a real Postgres server: `listen` is only ever defined on the
    asyncpg/rust_pg clients (never on SqliteClient), so attaching a fake one directly onto the
    router-resolved connection's instance - and nowhere else - deterministically proves which
    connection _run_listen_loop() actually consulted. Pre-fix, it reads the static default
    connection (no `listen` attribute at all) and bails out immediately with a one-time ERROR
    log, never calling the fake `listen` below even once.
    """
    ctx, RoutedOutboxEvent = routed_outbox
    router_target_db = ctx.connections.get("outbox_router_target")

    class DeadOnArrivalListener:
        def is_closed(self) -> bool:
            return True

        async def close(self) -> None:
            return None

    listen_calls: list[str] = []

    async def fake_listen(channel: str, callback: object) -> DeadOnArrivalListener:
        listen_calls.append(channel)
        return DeadOnArrivalListener()

    sleeps: list[float] = []

    async def fast_pause(self: NotificationListener, delay: float) -> None:
        sleeps.append(delay)

    relay = OutboxRelay(RoutedOutboxEvent, lambda event: None, listen_channel="relay_router_test_channel")

    with (
        mock.patch.object(router_target_db, "listen", fake_listen, create=True),
        mock.patch.object(NotificationListener, "pause", fast_pause),
    ):
        await relay._run_listen_loop()

    # LISTEN_MAX_RECONNECT_ATTEMPTS + 1: the initial connect plus every backoff-and-retry cycle,
    # matching test_relay_postgres.py's identical DeadOnArrivalListener convention - every single
    # one of them reached the router-resolved connection's fake `listen`, never the static
    # default connection (which has no `listen` attribute at all and would have returned after
    # the very first, un-retried check instead).
    assert len(listen_calls) == LISTEN_MAX_RECONNECT_ATTEMPTS + 1
    assert len(sleeps) == LISTEN_MAX_RECONNECT_ATTEMPTS
