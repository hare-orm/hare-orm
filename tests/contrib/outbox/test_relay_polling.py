"""Proves the polling backstop works standalone, with no wakeup involved at all - events
published before the relay is even started still all get delivered once it starts polling.
SQLite is fine here: its one writer claims a batch with no row locks at all."""

import asyncio
from datetime import timedelta

import pytest

from hare.contrib.outbox import OutboxRelay
from hare.time import Timezone
from tests.contrib.outbox.models import DemoOutboxEvent, SoftDeleteOutboxEvent, TenantScopedOutboxEvent
from tests.utils.waiting import wait_until

EVENT_COUNT = 5


@pytest.mark.asyncio
async def test_polling_delivers_events_published_before_the_relay_started(db_outbox):
    for i in range(EVENT_COUNT):
        await DemoOutboxEvent.enqueue("widget.updated", {"i": i})

    delivered: list[int] = []

    async def deliver(event: DemoOutboxEvent) -> None:
        delivered.append(event.payload["i"])

    relay = OutboxRelay(DemoOutboxEvent, deliver, poll_interval_seconds=0.05, batch_size=10)
    async with relay:
        await wait_until(lambda: len(delivered) == EVENT_COUNT)

    assert sorted(delivered) == list(range(EVENT_COUNT))
    published_count = await DemoOutboxEvent.objects.filter(published_at__isnull=False).count()
    assert published_count == EVENT_COUNT


@pytest.mark.asyncio
async def test_a_relay_of_an_outbox_named_by_its_label(db_outbox):
    await DemoOutboxEvent.enqueue("widget.updated", {"i": 1})
    delivered: list[int] = []

    async def deliver(event: DemoOutboxEvent) -> None:
        delivered.append(event.payload["i"])

    relay = OutboxRelay("models.DemoOutboxEvent", deliver)
    assert await relay.poll_once() == 1
    assert relay.model is DemoOutboxEvent
    assert delivered == [1]


@pytest.mark.asyncio
async def test_poll_once_respects_batch_size(db_outbox):
    for i in range(EVENT_COUNT):
        await DemoOutboxEvent.enqueue("widget.updated", {"i": i})

    delivered: list[int] = []

    async def deliver(event: DemoOutboxEvent) -> None:
        delivered.append(event.payload["i"])

    relay = OutboxRelay(DemoOutboxEvent, deliver, batch_size=2)
    claimed = await relay.poll_once()

    assert claimed == 2
    assert len(delivered) == 2


@pytest.mark.asyncio
async def test_poll_once_ignores_already_published_events(db_outbox):
    already_published = await DemoOutboxEvent.enqueue("widget.updated", {})
    already_published.published_at = already_published.created_at
    await already_published.save(update_fields=["published_at"])
    unpublished = await DemoOutboxEvent.enqueue("widget.updated", {})

    delivered: list[str] = []

    async def deliver(event: DemoOutboxEvent) -> None:
        delivered.append(str(event.id))

    relay = OutboxRelay(DemoOutboxEvent, deliver)
    claimed = await relay.poll_once()

    assert claimed == 1
    assert delivered == [str(unpublished.id)]


@pytest.mark.asyncio
async def test_poll_once_delivers_across_tenants_when_model_has_tenant_field(db_outbox):
    """OutboxRelay's own internal housekeeping queries (polling/claiming/cleanup) must service
    the WHOLE table regardless of Meta.tenant_field - it's framework machinery, never scoped
    inside Tenancy.scope(). Before the fix, the polling, claiming and
    cleanup_published() all routed through the model's own ambient tenant scoping, which raised
    ConfigurationError("no tenant is active") on every single poll for a tenant-scoped
    OutboxEvent subclass - the relay silently never delivered anything."""
    await TenantScopedOutboxEvent.objects.create(topic="widget.updated", payload={}, tenant_id=1)
    await TenantScopedOutboxEvent.objects.create(topic="widget.updated", payload={}, tenant_id=2)

    delivered: list[int] = []

    async def deliver(event: TenantScopedOutboxEvent) -> None:
        delivered.append(event.tenant_id)

    relay = OutboxRelay(TenantScopedOutboxEvent, deliver, batch_size=10)
    claimed = await relay.poll_once()

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
    event = await DemoOutboxEvent.enqueue("widget.updated", {"i": 0})
    delivered = asyncio.Event()

    async def deliver(delivered_event: DemoOutboxEvent) -> None:
        delivered.set()

    relay = OutboxRelay(DemoOutboxEvent, deliver, poll_interval_seconds=0.05, batch_size=10)
    await relay.start()
    await asyncio.wait_for(delivered.wait(), timeout=5)
    await relay.stop()

    await event.refresh_from_db()
    assert event.published_at is not None


@pytest.mark.asyncio
async def test_an_ordering_key_is_delivered_in_order_one_event_at_a_time(db_outbox):
    for index in range(3):
        await DemoOutboxEvent.enqueue("widget.updated", {"i": index}, ordering_key="widget:1")
    await DemoOutboxEvent.enqueue("widget.updated", {"i": 10})
    batches: list[list[int]] = []

    async def deliver(event: DemoOutboxEvent) -> None:
        batches[-1].append(event.payload["i"])

    relay = OutboxRelay(DemoOutboxEvent, deliver, batch_size=10)
    while True:
        batches.append([])
        if not await relay.poll_once():
            break

    # One event of the key per batch - in order; the event without a key goes with the first.
    assert batches == [[0, 10], [1], [2], []]


@pytest.mark.asyncio
async def test_a_live_lease_holds_an_event_and_an_expired_one_frees_it(db_outbox):
    event = await DemoOutboxEvent.enqueue("widget.updated", {})
    await DemoOutboxEvent.objects.filter(id=event.id).update(
        lease_until=Timezone.now() + timedelta(minutes=5), leased_by="another relay"
    )
    delivered: list[object] = []

    async def deliver(delivered_event: DemoOutboxEvent) -> None:
        delivered.append(delivered_event.leased_by)

    relay = OutboxRelay(DemoOutboxEvent, deliver, name="this relay")
    assert await relay.poll_once() == 0

    # The other relay stopped mid-delivery - its lease runs out and the event is delivered again.
    await DemoOutboxEvent.objects.filter(id=event.id).update(lease_until=Timezone.now() - timedelta(seconds=1))
    assert await relay.poll_once() == 1
    assert delivered == ["this relay"]
    await event.refresh_from_db()
    assert (event.lease_until, event.leased_by) == (None, None)
    assert event.published_at is not None


@pytest.mark.timing
@pytest.mark.asyncio
async def test_a_batch_outlasting_its_lease_keeps_it_and_is_delivered_once(db_outbox):
    for number in range(6):
        await DemoOutboxEvent.enqueue("widget.updated", {"n": number})
    deliveries: list[int] = []

    async def deliver(event: DemoOutboxEvent) -> None:
        deliveries.append(event.payload["n"])
        await asyncio.sleep(0.25)

    # A delivery has far more time than it takes - a slow machine doesn't time it out.
    options = {"concurrency": 1, "lease_seconds": 1.0, "delivery_timeout_seconds": 0.9}
    first = OutboxRelay(DemoOutboxEvent, deliver, name="first", **options)
    second = OutboxRelay(DemoOutboxEvent, deliver, name="second", **options)
    first_poll = asyncio.ensure_future(first.poll_once())
    # The batch takes 1.5 seconds - longer than its lease; the second relay polls after the first lease ran out.
    await asyncio.sleep(1.2)
    assert await second.poll_once() == 0
    assert await first_poll == 6
    assert sorted(deliveries) == [0, 1, 2, 3, 4, 5]
    assert await DemoOutboxEvent.objects.filter(published_at__isnull=True).count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("late_outcome", [RuntimeError("failed late"), None], ids=["failure", "success"])
async def test_an_outcome_recorded_after_another_relay_published_the_event_leaves_it_published(
    db_outbox, late_outcome
):
    """A relay whose lease ran out recorded its failure over the delivery of the relay that took the
    event over - the published event was dead-lettered too; its success wrote another publication time."""
    await DemoOutboxEvent.enqueue("widget.updated", {})
    late = OutboxRelay(DemoOutboxEvent, lambda event: None, name="late", max_delivery_attempts=1)
    events = await late.claim()
    published_at = Timezone.now() - timedelta(seconds=5)
    # The lease ran out; another relay leased the event and delivered it.
    await DemoOutboxEvent.objects.all().update(leased_by=None, lease_until=None, published_at=published_at)
    await late.record_outcomes(events, [late_outcome])
    event = await DemoOutboxEvent.objects.get()
    assert event.published_at == published_at
    assert (event.attempts, event.last_error, event.dead_lettered_at) == (0, None, None)


@pytest.mark.asyncio
async def test_a_failure_recorded_after_another_relay_leased_the_event_leaves_its_lease(db_outbox):
    await DemoOutboxEvent.enqueue("widget.updated", {})
    late = OutboxRelay(DemoOutboxEvent, lambda event: None, name="late")
    events = await late.claim()
    await DemoOutboxEvent.objects.all().update(leased_by="taking_over")
    await late.record_outcomes(events, [RuntimeError("failed late")])
    event = await DemoOutboxEvent.objects.get()
    assert (event.leased_by, event.attempts, event.last_error) == ("taking_over", 0, None)


@pytest.mark.asyncio
async def test_topics_limit_what_the_relay_claims_counts_and_cleans(db_outbox):
    await DemoOutboxEvent.enqueue("widget.updated", {})
    await DemoOutboxEvent.enqueue("gadget.updated", {})
    delivered: list[str] = []

    async def deliver(event: DemoOutboxEvent) -> None:
        delivered.append(event.topic)

    relay = OutboxRelay(DemoOutboxEvent, deliver, topics=["widget.updated"])
    assert (await relay.get_backlog()).pending == 1
    assert await relay.poll_once() == 1
    assert delivered == ["widget.updated"]
    assert (await relay.get_backlog()).pending == 0
    await DemoOutboxEvent.objects.all().update(published_at=Timezone.now() - timedelta(days=2))
    assert await relay.cleanup_published(timedelta(days=1)) == 1
    assert await DemoOutboxEvent.objects.filter(topic="gadget.updated").count() == 1


@pytest.mark.asyncio
async def test_the_backlog_tells_the_oldest_pending_age(db_outbox):
    relay = OutboxRelay(DemoOutboxEvent, lambda event: None)
    assert (await relay.get_backlog()).oldest_pending_age_seconds == 0.0
    await DemoOutboxEvent.enqueue("widget.updated", {})
    await DemoOutboxEvent.objects.all().update(created_at=Timezone.now() - timedelta(minutes=2))
    backlog = await relay.get_backlog()
    assert backlog.pending == 1
    assert backlog.oldest_pending_age_seconds >= 119
