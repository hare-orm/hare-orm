"""publish() writes an outbox row through the ordinary ORM connection-resolution machinery, with
no using= plumbing of its own - inside Transactions.atomic(), that means it lands on
the exact same connection/transaction as the caller's own business write, so it rolls back with
it for free. Plain transaction atomicity, not a Postgres-specific feature - sqlite is enough."""

import pytest

from hare.contrib.test import requires_features
from hare.transactions.transactions import Transactions
from tests.contrib.outbox.models import DemoOutboxEvent


class _BusinessError(Exception):
    pass


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_publish_rolls_back_with_the_enclosing_transaction(db_outbox):
    with pytest.raises(_BusinessError):
        async with Transactions.atomic():
            await DemoOutboxEvent.publish(topic="widget.updated", payload={"widget_id": 1})
            raise _BusinessError("business write failed after publish()")

    assert await DemoOutboxEvent.objects.all().count() == 0


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_publish_commits_with_the_enclosing_transaction(db_outbox):
    async with Transactions.atomic():
        event = await DemoOutboxEvent.publish(topic="widget.updated", payload={"widget_id": 1})

    persisted = await DemoOutboxEvent.objects.get(id=event.id)
    assert persisted.topic == "widget.updated"
    assert persisted.payload == {"widget_id": 1}
    assert persisted.published_at is None
    assert persisted.attempts == 0


@pytest.mark.asyncio
async def test_publish_outside_a_transaction_still_persists_immediately(db_outbox):
    event = await DemoOutboxEvent.publish(topic="widget.created", payload={})
    assert await DemoOutboxEvent.objects.get(id=event.id) is not None
