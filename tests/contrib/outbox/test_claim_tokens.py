"""A claim token, rather than the relay name, protects attempt state after lease handoff."""

from datetime import timedelta

import pytest

from hare.contrib.outbox import DeliveryResult, OutboxRelay
from hare.time import Timezone
from tests.contrib.outbox.models import SubscriberOutboxEvent


@pytest.mark.asyncio
@pytest.mark.parametrize("late_failure", [False, True], ids=["acknowledgement", "failure"])
async def test_old_claim_cannot_replace_attempt_metadata_even_with_same_relay_name(db_outbox, late_failure):
    event = await SubscriberOutboxEvent.enqueue(
        "book.changed", {}, extra_field_values={"subscriber_url": "https://example.invalid"}
    )

    async def deliver(event):
        pass

    relay = OutboxRelay(SubscriberOutboxEvent, deliver, name="shared-name")
    previous = await relay.claim()
    await SubscriberOutboxEvent.objects.filter(id=event.id).update(lease_until=None)
    current = await relay.claim()
    assert previous.token != current.token
    lease_until = Timezone.now() + timedelta(minutes=5)
    await SubscriberOutboxEvent.objects.filter(id=event.id).update(
        webhook_result={"attempt": "current"}, lease_until=lease_until
    )

    await relay.store.renew(previous, lease_seconds=1)
    await event.refresh_from_db()
    assert event.lease_until == lease_until

    await relay.record_outcomes(
        previous,
        [
            DeliveryResult(
                event.id, RuntimeError("late") if late_failure else None, {"webhook_result": {"attempt": "old"}}
            )
        ],
    )
    await event.refresh_from_db()
    assert event.webhook_result == {"attempt": "current"}
    assert event.attempts == 0
    assert event.last_error is None
    assert (event.published_at is None) is late_failure
    assert event.lease_token == (current.token if late_failure else None)


@pytest.mark.asyncio
async def test_current_failure_records_metadata_and_attempt_together(db_outbox):
    event = await SubscriberOutboxEvent.enqueue(
        "book.changed", {}, extra_field_values={"subscriber_url": "https://example.invalid"}
    )

    async def deliver(event):
        return DeliveryResult(event.id, RuntimeError("refused"), {"webhook_result": {"status_code": 503}})

    await OutboxRelay(SubscriberOutboxEvent, deliver).poll_once()
    await event.refresh_from_db()
    assert event.webhook_result == {"status_code": 503}
    assert event.attempts == 1
    assert event.last_error == "refused"
    assert event.lease_token is None
    assert event.published_at is None
