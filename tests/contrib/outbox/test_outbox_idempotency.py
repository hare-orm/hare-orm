"""OutboxEvent.enqueue(): idempotency_key, ordering_key, headers, extra_field_values and the wakeup."""

import asyncio

import pytest

from hare.contrib.outbox import OutboxWakeup
from hare.contrib.outbox.constants import OUTBOX_KEY_MAX_LENGTH
from hare.contrib.test import requires_features
from hare.exceptions import (
    IntegrityError,
    QueryError,
    ValidationError,
)
from hare.models.tenancy.tenancy import Tenancy
from hare.transactions.transactions import Transactions
from tests.contrib.outbox.models import (
    DemoOutboxEvent,
    SoftDeleteOutboxEvent,
    SourcedOutboxEvent,
    TenantScopedOutboxEvent,
)

CONNECTION_ALIAS = "models"
NO_DELIVERY_WAIT_SECONDS = 0.5


class RollBack(Exception):
    pass


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_republishing_the_same_key_returns_the_first_row(db_outbox):
    first = await DemoOutboxEvent.enqueue("widget.created", {"n": 1}, idempotency_key="widget-1")
    second = await DemoOutboxEvent.enqueue("widget.renamed", {"n": 2}, idempotency_key="widget-1")

    assert second.id == first.id
    assert second.topic == "widget.created"
    assert second.payload == {"n": 1}
    assert second.idempotency_key == "widget-1"
    assert await DemoOutboxEvent.objects.all().count() == 1


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_returned_new_row_is_fully_populated(db_outbox):
    event = await DemoOutboxEvent.enqueue("widget.created", {"n": 1}, idempotency_key="widget-1")
    assert event.created_at is not None
    assert event.published_at is None
    assert event.attempts == 0
    stored = await DemoOutboxEvent.objects.get(id=event.id)
    assert stored.idempotency_key == "widget-1"
    assert stored.payload == {"n": 1}


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_different_keys_create_different_rows(db_outbox):
    first = await DemoOutboxEvent.enqueue("widget.created", {}, idempotency_key="widget-1")
    second = await DemoOutboxEvent.enqueue("widget.created", {}, idempotency_key="widget-2")
    assert first.id != second.id
    assert await DemoOutboxEvent.objects.all().count() == 2


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_without_key_duplicates_are_still_allowed(db_outbox):
    """Several rows with a NULL idempotency_key never conflict with each other."""
    first = await DemoOutboxEvent.enqueue("widget.created", {"n": 1})
    second = await DemoOutboxEvent.enqueue("widget.created", {"n": 1})
    await DemoOutboxEvent.enqueue("widget.created", {"n": 1}, idempotency_key="widget-1")
    third = await DemoOutboxEvent.enqueue("widget.created", {"n": 1})

    assert len({first.id, second.id, third.id}) == 3
    assert first.idempotency_key is None
    assert await DemoOutboxEvent.objects.filter(idempotency_key=None).count() == 3


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_same_key_twice_inside_one_transaction(db_outbox):
    """The conflict must not abort the enclosing transaction (on Postgres an IntegrityError
    would): later statements and the commit still succeed."""
    async with Transactions.atomic(CONNECTION_ALIAS):
        first = await DemoOutboxEvent.enqueue("widget.created", {"n": 1}, idempotency_key="widget-1")
        second = await DemoOutboxEvent.enqueue("widget.created", {"n": 2}, idempotency_key="widget-1")
        assert second.id == first.id
        assert second.payload == {"n": 1}
        other = await DemoOutboxEvent.enqueue("widget.deleted", {})
        assert await DemoOutboxEvent.objects.all().count() == 2

    assert await DemoOutboxEvent.objects.all().count() == 2
    assert await DemoOutboxEvent.objects.get(id=other.id) is not None


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_same_key_in_separate_transactions(db_outbox):
    async with Transactions.atomic(CONNECTION_ALIAS):
        first = await DemoOutboxEvent.enqueue("widget.created", {"n": 1}, idempotency_key="widget-1")
    async with Transactions.atomic(CONNECTION_ALIAS):
        second = await DemoOutboxEvent.enqueue("widget.created", {"n": 2}, idempotency_key="widget-1")

    assert second.id == first.id
    assert second.payload == {"n": 1}
    assert await DemoOutboxEvent.objects.all().count() == 1


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_key_from_a_rolled_back_transaction_is_free_again(db_outbox):
    with pytest.raises(RollBack):
        async with Transactions.atomic(CONNECTION_ALIAS):
            await DemoOutboxEvent.enqueue("widget.created", {"n": 1}, idempotency_key="widget-1")
            raise RollBack

    event = await DemoOutboxEvent.enqueue("widget.created", {"n": 2}, idempotency_key="widget-1")
    assert event.payload == {"n": 2}
    assert await DemoOutboxEvent.objects.all().count() == 1


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_published_row_with_key_still_counts_as_existing(db_outbox):
    """Idempotency is about "already published once" - a delivered row still blocks the key."""
    first = await DemoOutboxEvent.enqueue("widget.created", {"n": 1}, idempotency_key="widget-1")
    await DemoOutboxEvent.objects.filter(id=first.id).update(attempts=1)
    second = await DemoOutboxEvent.enqueue("widget.created", {"n": 2}, idempotency_key="widget-1")
    assert second.id == first.id
    assert second.attempts == 1


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_soft_deleted_row_still_blocks_the_key(db_outbox):
    first = await SoftDeleteOutboxEvent.enqueue("widget.created", {"n": 1}, idempotency_key="widget-1")
    await first.delete()
    second = await SoftDeleteOutboxEvent.enqueue("widget.created", {"n": 2}, idempotency_key="widget-1")
    assert second.id == first.id
    assert second.payload == {"n": 1}


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_key_used_by_another_tenant_raises_instead_of_leaking_its_row(db_outbox):
    with Tenancy.scope(1):
        first = await TenantScopedOutboxEvent.enqueue("widget.created", {"n": 1}, idempotency_key="widget-1")
    with Tenancy.scope(2):
        with pytest.raises(IntegrityError, match="not visible"):
            await TenantScopedOutboxEvent.enqueue("widget.created", {"n": 2}, idempotency_key="widget-1")
    with Tenancy.scope(1):
        again = await TenantScopedOutboxEvent.enqueue("widget.created", {"n": 3}, idempotency_key="widget-1")
    assert again.id == first.id
    assert await TenantScopedOutboxEvent.objects.all_tenants().count() == 1


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_explicit_tenant_is_accepted_with_a_key_and_no_active_scope(db_outbox):
    """The keyed path used to require an active Tenancy.scope() even with the tenant given
    explicitly, unlike the unkeyed path (create())."""
    unkeyed = await TenantScopedOutboxEvent.enqueue("widget.created", {}, extra_field_values={"tenant_id": 5})
    keyed = await TenantScopedOutboxEvent.enqueue(
        "widget.created", {}, idempotency_key="widget-1", extra_field_values={"tenant_id": 5}
    )
    again = await TenantScopedOutboxEvent.enqueue(
        "widget.created", {}, idempotency_key="widget-1", extra_field_values={"tenant_id": 5}
    )

    assert unkeyed.tenant_id == keyed.tenant_id == 5
    assert again.id == keyed.id
    with pytest.raises(IntegrityError, match="not visible"):
        await TenantScopedOutboxEvent.enqueue(
            "widget.created", {}, idempotency_key="widget-1", extra_field_values={"tenant_id": 6}
        )
    with pytest.raises(QueryError, match="no tenant is active"):
        await TenantScopedOutboxEvent.enqueue("widget.created", {}, idempotency_key="widget-2")
    with Tenancy.scope(7), pytest.raises(QueryError, match="tenant other than the active one"):
        await TenantScopedOutboxEvent.enqueue(
            "widget.created", {}, idempotency_key="widget-3", extra_field_values={"tenant_id": 5}
        )
    assert await TenantScopedOutboxEvent.objects.all_tenants().count() == 2


@pytest.mark.parametrize(
    "options",
    [
        {"idempotency_key": ""},
        {"idempotency_key": "k" * (OUTBOX_KEY_MAX_LENGTH + 1)},
        {"idempotency_key": 42},
        {"ordering_key": ""},
        {"ordering_key": 7},
        {"headers": {"": 1}},
        {"headers": ["a"]},
    ],
)
@pytest.mark.asyncio
async def test_invalid_options_raise_before_writing(db_outbox, options):
    with pytest.raises(ValidationError):
        await DemoOutboxEvent.enqueue("widget.created", {}, **options)
    assert await DemoOutboxEvent.objects.all().count() == 0


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_key_of_maximum_length_is_accepted(db_outbox):
    key = "k" * OUTBOX_KEY_MAX_LENGTH
    event = await DemoOutboxEvent.enqueue("widget.created", {}, idempotency_key=key)
    assert (await DemoOutboxEvent.objects.get(id=event.id)).idempotency_key == key


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_concurrent_publish_with_same_key_waits_for_and_returns_the_first(db_outbox):
    """A second transaction's INSERT blocks on the first's uncommitted key; once the first
    commits it gets ON CONFLICT DO NOTHING and reads the committed row - no error, no dupe."""
    first_published = asyncio.Event()
    release_first = asyncio.Event()

    async def publish_first() -> DemoOutboxEvent:
        async with Transactions.atomic(CONNECTION_ALIAS):
            event = await DemoOutboxEvent.enqueue("widget.created", {"n": 1}, idempotency_key="widget-1")
            first_published.set()
            await release_first.wait()
            return event

    async def publish_second() -> DemoOutboxEvent:
        async with Transactions.atomic(CONNECTION_ALIAS):
            return await DemoOutboxEvent.enqueue("widget.created", {"n": 2}, idempotency_key="widget-1")

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


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_extra_field_values_fill_a_subclass_column_with_and_without_a_key(db_outbox):
    plain = await SourcedOutboxEvent.enqueue("widget.created", {"n": 1}, extra_field_values={"source": "admin"})
    keyed = await SourcedOutboxEvent.enqueue(
        "widget.created", {"n": 2}, idempotency_key="widget-2", extra_field_values={"source": "api"}
    )

    assert plain.source == "admin"
    assert keyed.source == "api"
    assert (await SourcedOutboxEvent.objects.get(id=keyed.id)).source == "api"


@pytest.mark.asyncio
async def test_extra_field_values_cannot_override_what_publish_sets_itself(db_outbox):
    with pytest.raises(ValidationError, match="topic"):
        await SourcedOutboxEvent.enqueue("widget.created", {}, extra_field_values={"topic": "other", "source": "x"})


class RecordingWakeup(OutboxWakeup):
    """Records every signal of written events."""

    def __init__(self) -> None:
        super().__init__()
        self.signalled: list[frozenset[str]] = []

    async def signal(self, topics: frozenset[str]) -> None:
        self.signalled.append(topics)

    async def listen(self, connection_alias: str) -> None:
        await asyncio.Event().wait()


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_a_duplicate_key_signals_no_wakeup(db_outbox):
    wakeup = RecordingWakeup()
    await DemoOutboxEvent.enqueue("widget.created", {}, idempotency_key="k", wakeup=wakeup)
    await DemoOutboxEvent.enqueue("widget.created", {}, idempotency_key="k", wakeup=wakeup)
    assert wakeup.signalled == [frozenset({"widget.created"})]


@requires_features(supports_transactions=True, supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_a_rolled_back_event_signals_no_wakeup(db_outbox):
    wakeup = RecordingWakeup()
    with pytest.raises(RollBack):
        async with Transactions.atomic(CONNECTION_ALIAS):
            await DemoOutboxEvent.enqueue("widget.created", {}, idempotency_key="k", wakeup=wakeup)
            raise RollBack
    assert wakeup.signalled == []
    assert await DemoOutboxEvent.objects.all().count() == 0


@pytest.mark.asyncio
async def test_the_model_wakeup_is_signalled_by_default(db_outbox):
    wakeup = RecordingWakeup()
    DemoOutboxEvent._meta.meta.outbox_wakeup = wakeup
    try:
        await DemoOutboxEvent.enqueue("widget.created", {})
    finally:
        DemoOutboxEvent._meta.meta.outbox_wakeup = None
    assert wakeup.signalled == [frozenset({"widget.created"})]


@pytest.mark.asyncio
async def test_ordering_key_and_headers_are_stored(db_outbox):
    event = await DemoOutboxEvent.enqueue(
        "widget.created", {"n": 1}, ordering_key="widget:1", headers={"traceparent": "00-ab-cd-01"}
    )
    stored = await DemoOutboxEvent.objects.get(id=event.id)
    assert stored.ordering_key == "widget:1"
    assert stored.headers == {"traceparent": "00-ab-cd-01"}
    assert stored.sequence == event.sequence
