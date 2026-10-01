"""OutboxEvent.publish() across a multi-connection (cross-app) boundary. Mirrors the exact
pattern of tests/test_cascade_cross_connection.py (dynamically-registered modules, two real
connections).

publish() used to resolve its own write connection purely through the model's own ambient
default_connection/router, with no check against whichever connection the caller's own
Transactions.atomic() was actually open on. A concrete OutboxEvent subclass configured on
a DIFFERENT connection than the business models it accompanies (e.g. one shared outbox table
living in its own database) silently lost the whole point of the transactional outbox pattern:
the outbox row committed standalone, immediately, even inside a transaction that later rolled
back for an unrelated reason - confirmed live, with zero error or warning anywhere.
"""

from __future__ import annotations

import os
import sys
import types

import pytest
import pytest_asyncio

from hare import fields
from hare.contrib.outbox.models import OutboxEvent
from hare.contrib.test import requires_features
from hare.exceptions import (
    QueryError,
)
from hare.models import Model
from hare.transactions.transactions import Transactions
from tests.utils.multi_database_context import MultiDatabaseTestContext

BIZ_MODULE_NAME = "tests._ccx_outbox_biz_models"
OUTBOX_MODULE_NAME = "tests._ccx_outbox_event_models"


class _UnrelatedBusinessError(Exception):
    pass


def _build_cross_connection_models() -> tuple[type[Model], type[OutboxEvent]]:
    class CcxOutboxWidget(Model):
        id = fields.IntField(primary_key=True)
        name = fields.TextField()

        class Meta:
            app = "ccx_outbox_biz"

    class CcxOutboxEvent(OutboxEvent):
        class Meta(OutboxEvent.Meta):
            app = "ccx_outbox_event"

    return CcxOutboxWidget, CcxOutboxEvent


@pytest_asyncio.fixture
async def cross_conn_outbox():
    CcxOutboxWidget, CcxOutboxEvent = _build_cross_connection_models()

    biz_module = types.ModuleType(BIZ_MODULE_NAME)
    setattr(biz_module, "CcxOutboxWidget", CcxOutboxWidget)  # noqa: B010
    outbox_module = types.ModuleType(OUTBOX_MODULE_NAME)
    setattr(outbox_module, "CcxOutboxEvent", CcxOutboxEvent)  # noqa: B010
    sys.modules[BIZ_MODULE_NAME] = biz_module
    sys.modules[OUTBOX_MODULE_NAME] = outbox_module

    db_url = os.getenv("HARE_TEST_DB", "sqlite://:memory:")
    try:
        async with MultiDatabaseTestContext.open(
            db_url,
            ["ccx_outbox_biz", "ccx_outbox_event"],
            apps={
                "ccx_outbox_biz": {"models": [BIZ_MODULE_NAME], "default_connection": "ccx_outbox_biz"},
                "ccx_outbox_event": {"models": [OUTBOX_MODULE_NAME], "default_connection": "ccx_outbox_event"},
            },
        ) as ctx:
            await ctx.generate_schemas()
            yield ctx, CcxOutboxWidget, CcxOutboxEvent
    finally:
        sys.modules.pop(BIZ_MODULE_NAME, None)
        sys.modules.pop(OUTBOX_MODULE_NAME, None)


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_publish_on_a_different_connection_than_the_active_transaction_raises(cross_conn_outbox):
    """Regression test: publish() used to silently write standalone (autocommitting immediately)
    when its own OutboxEvent subclass lived on a different connection than the caller's active
    transaction, instead of raising - the outbox row then survived the enclosing transaction's
    later rollback, even though the business write it was meant to describe never happened."""
    _ctx, CcxOutboxWidget, CcxOutboxEvent = cross_conn_outbox

    with pytest.raises(QueryError, match="currently does"):
        async with Transactions.atomic(using="ccx_outbox_biz"):
            widget = await CcxOutboxWidget.objects.create(id=1, name="Left Handle")
            await CcxOutboxEvent.publish(topic="widget.created", payload={"widget_id": widget.id})
            raise _UnrelatedBusinessError("should never be reached - publish() should raise first")

    assert await CcxOutboxWidget.objects.all().count() == 0
    assert await CcxOutboxEvent.objects.all().count() == 0


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_publish_with_explicit_using_db_bypasses_the_check(cross_conn_outbox):
    """The escape hatch: explicitly passing using= (here, the outbox model's OWN connection,
    opened as its own transaction alongside the business one) opts out of the guard entirely -
    for a caller who genuinely knows the two connections can't share real SQL atomicity here and
    has their own compensating strategy for it."""
    _ctx, _CcxOutboxWidget, CcxOutboxEvent = cross_conn_outbox

    async with Transactions.atomic(using="ccx_outbox_biz"):
        async with Transactions.atomic(using="ccx_outbox_event") as outbox_connection:
            event = await CcxOutboxEvent.publish(
                topic="widget.created", payload={"widget_id": 1}, using=outbox_connection
            )

    assert await CcxOutboxEvent.objects.get(id=event.id) is not None


@pytest.mark.asyncio
async def test_publish_with_no_active_transaction_anywhere_still_works(cross_conn_outbox):
    """No active transaction on ANY connection at all - publish() must still work exactly as
    documented (commits immediately, standalone), not raise just because it lives on its own
    connection."""
    _ctx, _CcxOutboxWidget, CcxOutboxEvent = cross_conn_outbox

    event = await CcxOutboxEvent.publish(topic="widget.created", payload={"widget_id": 1})
    assert await CcxOutboxEvent.objects.get(id=event.id) is not None


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_publish_with_idempotency_key_still_raises_on_a_different_connection(cross_conn_outbox):
    """The wrong-transaction guard also covers the idempotency_key=/notify_channel= path -
    nothing is written or sent before it raises."""
    _ctx, CcxOutboxWidget, CcxOutboxEvent = cross_conn_outbox

    with pytest.raises(QueryError, match="currently does"):
        async with Transactions.atomic(using="ccx_outbox_biz"):
            await CcxOutboxWidget.objects.create(id=1, name="Left Handle")
            await CcxOutboxEvent.publish(
                topic="widget.created",
                payload={"widget_id": 1},
                idempotency_key="widget-1-created",
                notify_channel="hare_test_ccx_outbox",
            )

    assert await CcxOutboxEvent.objects.all().count() == 0
