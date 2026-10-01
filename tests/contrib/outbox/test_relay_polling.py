"""Proves the polling backstop works standalone, with no LISTEN/NOTIFY involved at all - events
published before the relay is even started still all get delivered once it starts polling.
SQLite is fine here: OutboxRelay._poll_once() only attempts SELECT ... FOR UPDATE SKIP LOCKED on
a backend that supports it (Postgres) - see test_relay_dead_letter.py's docstring for the
single-relay-instance assumption this relies on on SQLite."""

import asyncio
from datetime import timedelta

import pytest

from hare.contrib.outbox import OutboxRelay
from hare.utils import Timezone
from tests.contrib.outbox.models import DemoOutboxEvent, SoftDeleteOutboxEvent, TenantScopedOutboxEvent

EVENT_COUNT = 5


@pytest.mark.asyncio
async def test_polling_delivers_events_published_before_the_relay_started(db_outbox):
    for i in range(EVENT_COUNT):
        await DemoOutboxEvent.publish(topic="widget.updated", payload={"i": i})

    delivered: list[int] = []

    async def deliver(event: DemoOutboxEvent) -> None:
        delivered.append(event.payload["i"])

    relay = OutboxRelay(DemoOutboxEvent, deliver, poll_interval_seconds=0.05, batch_size=10)
    async with relay:
        for _ in range(100):
            if len(delivered) == EVENT_COUNT:
                break
            await asyncio.sleep(0.05)

    assert sorted(delivered) == list(range(EVENT_COUNT))
    published_count = await DemoOutboxEvent.objects.filter(published_at__isnull=False).count()
    assert published_count == EVENT_COUNT


@pytest.mark.asyncio
async def test_poll_once_respects_batch_size(db_outbox):
    for i in range(EVENT_COUNT):
        await DemoOutboxEvent.publish(topic="widget.updated", payload={"i": i})

    delivered: list[int] = []

    async def deliver(event: DemoOutboxEvent) -> None:
        delivered.append(event.payload["i"])

    relay = OutboxRelay(DemoOutboxEvent, deliver, batch_size=2)
    claimed = await relay._poll_once()

    assert claimed == 2
    assert len(delivered) == 2


@pytest.mark.asyncio
async def test_poll_once_ignores_already_published_events(db_outbox):
    already_published = await DemoOutboxEvent.publish(topic="widget.updated", payload={})
    already_published.published_at = already_published.created_at
    await already_published.save(update_fields=["published_at"])
    unpublished = await DemoOutboxEvent.publish(topic="widget.updated", payload={})

    delivered: list[str] = []

    async def deliver(event: DemoOutboxEvent) -> None:
        delivered.append(str(event.id))

    relay = OutboxRelay(DemoOutboxEvent, deliver)
    claimed = await relay._poll_once()

    assert claimed == 1
    assert delivered == [str(unpublished.id)]


@pytest.mark.asyncio
async def test_poll_once_delivers_across_tenants_when_model_has_tenant_field(db_outbox):
    """OutboxRelay's own internal housekeeping queries (polling/claiming/cleanup) must service
    the WHOLE table regardless of Meta.tenant_field - it's framework machinery, never scoped
    inside Tenancy.scope(). Before the fix, _poll_once()/_claim_and_deliver_one()/
    cleanup_published() all routed through the model's own ambient tenant scoping, which raised
    ConfigurationError("no tenant is active") on every single poll for a tenant-scoped
    OutboxEvent subclass - the relay silently never delivered anything."""
    await TenantScopedOutboxEvent.objects.create(topic="widget.updated", payload={}, tenant_id=1)
    await TenantScopedOutboxEvent.objects.create(topic="widget.updated", payload={}, tenant_id=2)

    delivered: list[int] = []

    async def deliver(event: TenantScopedOutboxEvent) -> None:
        delivered.append(event.tenant_id)

    relay = OutboxRelay(TenantScopedOutboxEvent, deliver, batch_size=10)
    claimed = await relay._poll_once()

    assert claimed == 2
    assert sorted(delivered) == [1, 2]


@pytest.mark.asyncio
async def test_cleanup_published_spans_every_tenant_when_model_has_tenant_field(db_outbox):
    old_cutoff = Timezone.now() - timedelta(days=1)
    event = await TenantScopedOutboxEvent.objects.create(topic="widget.updated", payload={}, tenant_id=1)
    event.published_at = old_cutoff
    await event.save(update_fields=["published_at"])

    async def deliver(_event: TenantScopedOutboxEvent) -> None:
        pass

    relay = OutboxRelay(TenantScopedOutboxEvent, deliver)
    deleted = await relay.cleanup_published(older_than=timedelta(hours=1))

    assert deleted == 1


@pytest.mark.asyncio
async def test_cleanup_published_rejects_a_soft_delete_enabled_model(db_outbox):
    """QuerySet.delete() turns into a soft-delete UPDATE for a Meta.soft_delete_field model -
    cleanup_published() must refuse this combination up front (a clear, loud error) rather than
    silently soft-deleting instead of actually reclaiming storage, its whole documented purpose,
    with the outbox table's real row count never actually shrinking despite periodic cleanup
    calls succeeding with no error at all."""
    from hare.exceptions import ConfigurationError

    old_cutoff = Timezone.now() - timedelta(days=1)
    event = await SoftDeleteOutboxEvent.objects.create(topic="widget.updated", payload={})
    event.published_at = old_cutoff
    await event.save(update_fields=["published_at"])

    async def deliver(_event: SoftDeleteOutboxEvent) -> None:
        pass

    relay = OutboxRelay(SoftDeleteOutboxEvent, deliver)
    with pytest.raises(ConfigurationError, match="soft_delete_field"):
        await relay.cleanup_published(older_than=timedelta(hours=1))

    assert await SoftDeleteOutboxEvent.objects.all().count() == 1


@pytest.mark.asyncio
async def test_stop_right_after_a_successful_delivery_still_marks_it_published(db_outbox):
    """Bug: stop() cancelling between a successful deliver() and the commit of published_at left
    the already-delivered row unmarked, so the next relay run delivered it a second time."""
    event = await DemoOutboxEvent.publish(topic="widget.updated", payload={"i": 0})
    delivered = asyncio.Event()

    async def deliver(delivered_event: DemoOutboxEvent) -> None:
        delivered.set()

    relay = OutboxRelay(DemoOutboxEvent, deliver, poll_interval_seconds=0.05, batch_size=10)
    await relay.start()
    await asyncio.wait_for(delivered.wait(), timeout=5)
    await relay.stop()

    await event.refresh_from_db()
    assert event.published_at is not None
