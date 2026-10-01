"""OutboxEvent.publish(idempotency_key=..., notify_channel=...)."""

import asyncio
import logging

import pytest

from hare import Connections
from hare.contrib.notify import NotificationListener
from hare.contrib.outbox import OutboxRelay
from hare.contrib.outbox.constants import IDEMPOTENCY_KEY_MAX_LENGTH
from hare.contrib.test import requires_features
from hare.exceptions import (
    IntegrityError,
    QueryError,
    ValidationError,
)
from hare.models.tenancy import Tenancy
from hare.transactions.transactions import Transactions
from tests.contrib.outbox.models import (
    DemoOutboxEvent,
    SoftDeleteOutboxEvent,
    SourcedOutboxEvent,
    TenantScopedOutboxEvent,
)

CONNECTION_ALIAS = "models"
NO_DELIVERY_WAIT_SECONDS = 0.5


class Collector:
    """Records received NOTIFY payloads and lets a test await the next one."""

    def __init__(self) -> None:
        self.payloads: list[str] = []
        self.received = asyncio.Event()

    def __call__(self, payload: str) -> None:
        self.payloads.append(payload)
        self.received.set()

    async def wait(self, timeout: float = 5) -> None:
        await asyncio.wait_for(self.received.wait(), timeout=timeout)
        self.received.clear()


class RollBack(Exception):
    pass


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_republishing_the_same_key_returns_the_first_row(db_outbox):
    first = await DemoOutboxEvent.publish("widget.created", {"n": 1}, idempotency_key="widget-1")
    second = await DemoOutboxEvent.publish("widget.renamed", {"n": 2}, idempotency_key="widget-1")

    assert second.id == first.id
    assert second.topic == "widget.created"
    assert second.payload == {"n": 1}
    assert second.idempotency_key == "widget-1"
    assert await DemoOutboxEvent.objects.all().count() == 1


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_returned_new_row_is_fully_populated(db_outbox):
    event = await DemoOutboxEvent.publish("widget.created", {"n": 1}, idempotency_key="widget-1")
    assert event.created_at is not None
    assert event.published_at is None
    assert event.attempts == 0
    stored = await DemoOutboxEvent.objects.get(id=event.id)
    assert stored.idempotency_key == "widget-1"
    assert stored.payload == {"n": 1}


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_different_keys_create_different_rows(db_outbox):
    first = await DemoOutboxEvent.publish("widget.created", {}, idempotency_key="widget-1")
    second = await DemoOutboxEvent.publish("widget.created", {}, idempotency_key="widget-2")
    assert first.id != second.id
    assert await DemoOutboxEvent.objects.all().count() == 2


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_without_key_duplicates_are_still_allowed(db_outbox):
    """Several rows with a NULL idempotency_key never conflict with each other."""
    first = await DemoOutboxEvent.publish("widget.created", {"n": 1})
    second = await DemoOutboxEvent.publish("widget.created", {"n": 1})
    await DemoOutboxEvent.publish("widget.created", {"n": 1}, idempotency_key="widget-1")
    third = await DemoOutboxEvent.publish("widget.created", {"n": 1})

    assert len({first.id, second.id, third.id}) == 3
    assert first.idempotency_key is None
    assert await DemoOutboxEvent.objects.filter(idempotency_key=None).count() == 3


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_same_key_twice_inside_one_transaction(db_outbox):
    """The conflict must not abort the enclosing transaction (on Postgres an IntegrityError
    would): later statements and the commit still succeed."""
    async with Transactions.atomic(CONNECTION_ALIAS):
        first = await DemoOutboxEvent.publish("widget.created", {"n": 1}, idempotency_key="widget-1")
        second = await DemoOutboxEvent.publish("widget.created", {"n": 2}, idempotency_key="widget-1")
        assert second.id == first.id
        assert second.payload == {"n": 1}
        other = await DemoOutboxEvent.publish("widget.deleted", {})
        assert await DemoOutboxEvent.objects.all().count() == 2

    assert await DemoOutboxEvent.objects.all().count() == 2
    assert await DemoOutboxEvent.objects.get(id=other.id) is not None


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_same_key_in_separate_transactions(db_outbox):
    async with Transactions.atomic(CONNECTION_ALIAS):
        first = await DemoOutboxEvent.publish("widget.created", {"n": 1}, idempotency_key="widget-1")
    async with Transactions.atomic(CONNECTION_ALIAS):
        second = await DemoOutboxEvent.publish("widget.created", {"n": 2}, idempotency_key="widget-1")

    assert second.id == first.id
    assert second.payload == {"n": 1}
    assert await DemoOutboxEvent.objects.all().count() == 1


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_key_from_a_rolled_back_transaction_is_free_again(db_outbox):
    with pytest.raises(RollBack):
        async with Transactions.atomic(CONNECTION_ALIAS):
            await DemoOutboxEvent.publish("widget.created", {"n": 1}, idempotency_key="widget-1")
            raise RollBack

    event = await DemoOutboxEvent.publish("widget.created", {"n": 2}, idempotency_key="widget-1")
    assert event.payload == {"n": 2}
    assert await DemoOutboxEvent.objects.all().count() == 1


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_published_row_with_key_still_counts_as_existing(db_outbox):
    """Idempotency is about "already published once" - a delivered row still blocks the key."""
    first = await DemoOutboxEvent.publish("widget.created", {"n": 1}, idempotency_key="widget-1")
    await DemoOutboxEvent.objects.filter(id=first.id).update(attempts=1)
    second = await DemoOutboxEvent.publish("widget.created", {"n": 2}, idempotency_key="widget-1")
    assert second.id == first.id
    assert second.attempts == 1


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_soft_deleted_row_still_blocks_the_key(db_outbox):
    first = await SoftDeleteOutboxEvent.publish("widget.created", {"n": 1}, idempotency_key="widget-1")
    await first.delete()
    second = await SoftDeleteOutboxEvent.publish("widget.created", {"n": 2}, idempotency_key="widget-1")
    assert second.id == first.id
    assert second.payload == {"n": 1}


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_key_used_by_another_tenant_raises_instead_of_leaking_its_row(db_outbox):
    with Tenancy.scope(1):
        first = await TenantScopedOutboxEvent.publish("widget.created", {"n": 1}, idempotency_key="widget-1")
    with Tenancy.scope(2):
        with pytest.raises(IntegrityError, match="not visible"):
            await TenantScopedOutboxEvent.publish("widget.created", {"n": 2}, idempotency_key="widget-1")
    with Tenancy.scope(1):
        again = await TenantScopedOutboxEvent.publish("widget.created", {"n": 3}, idempotency_key="widget-1")
    assert again.id == first.id
    assert await TenantScopedOutboxEvent.objects.all_tenants().count() == 1


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_explicit_tenant_is_accepted_with_a_key_and_no_active_scope(db_outbox):
    """The keyed path used to require an active Tenancy.scope() even with the tenant given
    explicitly, unlike the unkeyed path (create())."""
    unkeyed = await TenantScopedOutboxEvent.publish("widget.created", {}, extra_field_values={"tenant_id": 5})
    keyed = await TenantScopedOutboxEvent.publish(
        "widget.created", {}, idempotency_key="widget-1", extra_field_values={"tenant_id": 5}
    )
    again = await TenantScopedOutboxEvent.publish(
        "widget.created", {}, idempotency_key="widget-1", extra_field_values={"tenant_id": 5}
    )

    assert unkeyed.tenant_id == keyed.tenant_id == 5
    assert again.id == keyed.id
    with pytest.raises(IntegrityError, match="not visible"):
        await TenantScopedOutboxEvent.publish(
            "widget.created", {}, idempotency_key="widget-1", extra_field_values={"tenant_id": 6}
        )
    with pytest.raises(QueryError, match="no tenant is active"):
        await TenantScopedOutboxEvent.publish("widget.created", {}, idempotency_key="widget-2")
    with Tenancy.scope(7), pytest.raises(QueryError, match="tenant other than the active one"):
        await TenantScopedOutboxEvent.publish(
            "widget.created", {}, idempotency_key="widget-3", extra_field_values={"tenant_id": 5}
        )
    assert await TenantScopedOutboxEvent.objects.all_tenants().count() == 2


@pytest.mark.parametrize(
    "options",
    [
        {"idempotency_key": ""},
        {"idempotency_key": "k" * (IDEMPOTENCY_KEY_MAX_LENGTH + 1)},
        {"idempotency_key": 42},
        {"notify_channel": ""},
        {"notify_channel": 1},
    ],
)
@pytest.mark.asyncio
async def test_invalid_options_raise_before_writing(db_outbox, options):
    with pytest.raises(ValidationError):
        await DemoOutboxEvent.publish("widget.created", {}, **options)
    assert await DemoOutboxEvent.objects.all().count() == 0


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_key_of_maximum_length_is_accepted(db_outbox):
    key = "k" * IDEMPOTENCY_KEY_MAX_LENGTH
    event = await DemoOutboxEvent.publish("widget.created", {}, idempotency_key=key)
    assert (await DemoOutboxEvent.objects.get(id=event.id)).idempotency_key == key


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_notify_channel_is_ignored_on_sqlite(db_outbox, caplog):
    with caplog.at_level(logging.WARNING, logger="hare"):
        event = await DemoOutboxEvent.publish("widget.created", {}, notify_channel="hare_test_outbox_notify")
    assert await DemoOutboxEvent.objects.get(id=event.id) is not None
    assert caplog.records == []


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_concurrent_publish_with_same_key_waits_for_and_returns_the_first(db_outbox):
    """A second transaction's INSERT blocks on the first's uncommitted key; once the first
    commits it gets ON CONFLICT DO NOTHING and reads the committed row - no error, no dupe."""
    first_published = asyncio.Event()
    release_first = asyncio.Event()

    async def publish_first() -> DemoOutboxEvent:
        async with Transactions.atomic(CONNECTION_ALIAS):
            event = await DemoOutboxEvent.publish("widget.created", {"n": 1}, idempotency_key="widget-1")
            first_published.set()
            await release_first.wait()
            return event

    async def publish_second() -> DemoOutboxEvent:
        async with Transactions.atomic(CONNECTION_ALIAS):
            return await DemoOutboxEvent.publish("widget.created", {"n": 2}, idempotency_key="widget-1")

    first_task = asyncio.create_task(publish_first())
    await asyncio.wait_for(first_published.wait(), timeout=5)
    second_task = asyncio.create_task(publish_second())
    await asyncio.sleep(NO_DELIVERY_WAIT_SECONDS)
    assert not second_task.done()

    release_first.set()
    first = await asyncio.wait_for(first_task, timeout=5)
    second = await asyncio.wait_for(second_task, timeout=5)
    assert second.id == first.id
    assert second.payload == {"n": 1}
    assert await DemoOutboxEvent.objects.all().count() == 1


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_notify_channel_is_sent_only_on_commit(db_outbox):
    channel = "hare_test_outbox_publish_notify_commit"
    collector = Collector()
    async with NotificationListener(CONNECTION_ALIAS, channel, collector):
        async with Transactions.atomic(CONNECTION_ALIAS):
            event = await DemoOutboxEvent.publish("widget.created", {}, notify_channel=channel)
            await asyncio.sleep(NO_DELIVERY_WAIT_SECONDS)
            assert collector.payloads == []
        await collector.wait()
    assert collector.payloads == [str(event.id)]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_notify_channel_is_not_sent_on_rollback(db_outbox):
    channel = "hare_test_outbox_publish_notify_rollback"
    collector = Collector()
    async with NotificationListener(CONNECTION_ALIAS, channel, collector):
        with pytest.raises(RollBack):
            async with Transactions.atomic(CONNECTION_ALIAS):
                await DemoOutboxEvent.publish("widget.created", {}, idempotency_key="k", notify_channel=channel)
                raise RollBack
        await asyncio.sleep(NO_DELIVERY_WAIT_SECONDS)
        assert collector.payloads == []
    assert await DemoOutboxEvent.objects.all().count() == 0


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_notify_channel_outside_a_transaction_is_sent_immediately(db_outbox):
    channel = "hare_test_outbox_publish_notify_autocommit"
    collector = Collector()
    async with NotificationListener(CONNECTION_ALIAS, channel, collector):
        event = await DemoOutboxEvent.publish("widget.created", {}, idempotency_key="k", notify_channel=channel)
        await collector.wait()
    assert collector.payloads == [str(event.id)]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_duplicate_publish_does_not_notify_again(db_outbox):
    channel = "hare_test_outbox_publish_notify_duplicate"
    collector = Collector()
    async with NotificationListener(CONNECTION_ALIAS, channel, collector):
        await DemoOutboxEvent.publish("widget.created", {}, idempotency_key="k", notify_channel=channel)
        await collector.wait()
        await DemoOutboxEvent.publish("widget.created", {}, idempotency_key="k", notify_channel=channel)
        await asyncio.sleep(NO_DELIVERY_WAIT_SECONDS)
    assert len(collector.payloads) == 1


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_notify_channel_wakes_a_listening_relay_immediately(db_outbox):
    """poll_interval_seconds far exceeds the timeout, so delivery can only come via NOTIFY."""
    channel = "hare_test_outbox_publish_wakes_relay"
    delivered = asyncio.Event()
    delivered_ids: list[object] = []

    async def deliver(event: DemoOutboxEvent) -> None:
        delivered_ids.append(event.id)
        delivered.set()

    async with OutboxRelay(DemoOutboxEvent, deliver, poll_interval_seconds=30, listen_channel=channel):
        await asyncio.sleep(0.3)  # let the first (empty) poll run and LISTEN connect
        async with Transactions.atomic(CONNECTION_ALIAS):
            event = await DemoOutboxEvent.publish(
                "widget.created", {}, idempotency_key="widget-1", notify_channel=channel
            )
        await asyncio.wait_for(delivered.wait(), timeout=5)

    assert delivered_ids == [event.id]
    assert Connections.get(CONNECTION_ALIAS).features.supports_listen_notify


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_extra_field_values_fill_a_subclass_column_with_and_without_a_key(db_outbox):
    plain = await SourcedOutboxEvent.publish("widget.created", {"n": 1}, extra_field_values={"source": "admin"})
    keyed = await SourcedOutboxEvent.publish(
        "widget.created", {"n": 2}, idempotency_key="widget-2", extra_field_values={"source": "api"}
    )

    assert plain.source == "admin"
    assert keyed.source == "api"
    assert (await SourcedOutboxEvent.objects.get(id=keyed.id)).source == "api"


@pytest.mark.asyncio
async def test_extra_field_values_cannot_override_what_publish_sets_itself(db_outbox):
    with pytest.raises(ValidationError, match="topic"):
        await SourcedOutboxEvent.publish("widget.created", {}, extra_field_values={"topic": "other", "source": "x"})
