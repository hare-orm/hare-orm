"""PostgreSQL only: the LISTEN/NOTIFY wakeup, and relays sharing one queue - FOR UPDATE SKIP LOCKED
claims never deliver an event twice, and an ordering key stays in order across relays."""

import asyncio

import pytest

from hare.contrib.outbox import ListenNotifyWakeup, OutboxRelay
from hare.contrib.test import requires_features
from hare.transactions.transactions import Transactions
from tests.contrib.outbox.models import DemoOutboxEvent, TenantScopedOutboxEvent


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_a_notify_wakeup_delivers_long_before_the_poll_interval(db_outbox):
    wakeup = ListenNotifyWakeup(channel="hare_test_outbox_wakeup")
    delivered = asyncio.Event()

    async def deliver(_event: DemoOutboxEvent) -> None:
        delivered.set()

    async with OutboxRelay(DemoOutboxEvent, deliver, poll_interval_seconds=60, wakeup=wakeup):
        await asyncio.sleep(0.3)  # the LISTEN connects
        async with Transactions.atomic():
            event = await DemoOutboxEvent.enqueue("widget.updated", {}, wakeup=wakeup)
        await asyncio.wait_for(delivered.wait(), timeout=5)

    await event.refresh_from_db()
    assert event.published_at is not None


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_a_notify_goes_out_only_on_commit(db_outbox):
    wakeup = ListenNotifyWakeup(channel="hare_test_outbox_commit")
    signals: list[frozenset[str]] = []
    await wakeup.subscribe(signals.append, DemoOutboxEvent.get_connection(for_write=True).connection_alias)
    try:
        await asyncio.sleep(0.3)
        with pytest.raises(RuntimeError):
            async with Transactions.atomic():
                await DemoOutboxEvent.enqueue("widget.rolled_back", {}, wakeup=wakeup)
                raise RuntimeError("rolled back")
        async with Transactions.atomic():
            await DemoOutboxEvent.enqueue("widget.committed", {}, wakeup=wakeup)
            await DemoOutboxEvent.enqueue("widget.committed", {}, wakeup=wakeup)
        for _ in range(100):
            if signals:
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.1)
    finally:
        await wakeup.unsubscribe(signals.append)

    assert signals == [frozenset({"widget.committed"})]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_concurrent_relays_never_deliver_an_event_twice(db_outbox):
    event_count = 40
    for index in range(event_count):
        await DemoOutboxEvent.enqueue("widget.updated", {"i": index})
    delivered: list[int] = []

    async def deliver(event: DemoOutboxEvent) -> None:
        await asyncio.sleep(0.005)
        delivered.append(event.payload["i"])

    relays = [OutboxRelay(DemoOutboxEvent, deliver, batch_size=5, name=f"relay-{index}") for index in range(4)]

    async def drain(relay: OutboxRelay) -> None:
        while await relay.poll_once():
            pass

    await asyncio.gather(*(drain(relay) for relay in relays))

    assert sorted(delivered) == list(range(event_count))
    assert await DemoOutboxEvent.objects.filter(published_at__isnull=True).count() == 0


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_concurrent_relays_keep_an_ordering_key_in_order(db_outbox):
    for index in range(10):
        await DemoOutboxEvent.enqueue("widget.updated", {"i": index}, ordering_key="widget:1")
    delivered: list[int] = []

    async def deliver(event: DemoOutboxEvent) -> None:
        await asyncio.sleep(0.005)
        delivered.append(event.payload["i"])

    relays = [OutboxRelay(DemoOutboxEvent, deliver, batch_size=5, name=f"relay-{index}") for index in range(3)]

    async def drain(relay: OutboxRelay) -> None:
        for _ in range(50):
            await relay.poll_once()

    await asyncio.gather(*(drain(relay) for relay in relays))

    assert delivered == list(range(10))


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_a_claim_spans_every_tenant(db_outbox):
    await TenantScopedOutboxEvent.objects.create(topic="widget.updated", payload={}, tenant_id=1)
    await TenantScopedOutboxEvent.objects.create(topic="widget.updated", payload={}, tenant_id=2)
    delivered: list[int] = []

    async def deliver(event: TenantScopedOutboxEvent) -> None:
        delivered.append(event.tenant_id)

    relay = OutboxRelay(TenantScopedOutboxEvent, deliver, batch_size=10)
    assert await relay.poll_once() == 2
    assert sorted(delivered) == [1, 2]
