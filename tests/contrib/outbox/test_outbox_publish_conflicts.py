"""OutboxEvent.enqueue(): only an idempotency_key conflict is deduplicated, reserved fields."""

import uuid

import pytest

from hare.contrib.outbox.constants import RELAY_MANAGED_FIELD_NAMES
from hare.contrib.test import requires_features
from hare.exceptions import ConfigurationError, IntegrityError, QueryError, ValidationError
from hare.models.tenancy.tenancy import Tenancy
from hare.transactions.transactions import Transactions
from tests.contrib.outbox.models import (
    DemoOutboxEvent,
    NonUniqueKeyOutboxEvent,
    TenantKeyOutboxEvent,
    UniqueReferenceOutboxEvent,
)

CONNECTION_ALIAS = "models"
NO_DELIVERY_WAIT_SECONDS = 0.5


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_unique_subclass_column_conflict_with_a_new_key_raises(db_outbox):
    await UniqueReferenceOutboxEvent.enqueue(
        "widget.created", {"n": 1}, extra_field_values={"external_reference": "R1"}
    )

    with pytest.raises(IntegrityError):
        await UniqueReferenceOutboxEvent.enqueue(
            "widget.created", {"n": 2}, idempotency_key="new-key", extra_field_values={"external_reference": "R1"}
        )
    assert await UniqueReferenceOutboxEvent.objects.filter(idempotency_key="new-key").count() == 0


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_primary_key_conflict_with_a_new_key_raises(db_outbox):
    existing = await DemoOutboxEvent.enqueue("widget.created", {"n": 1})

    with pytest.raises(IntegrityError):
        await DemoOutboxEvent.enqueue(
            "widget.created", {"n": 2}, idempotency_key="new-key", extra_field_values={"id": existing.id}
        )
    assert await DemoOutboxEvent.objects.all().count() == 1


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_same_key_and_explicit_id_again_returns_the_existing_row_as_not_new(db_outbox):
    explicit_id = uuid.uuid4()
    write_connection = DemoOutboxEvent.get_connection(for_write=True)

    first, first_is_new = await DemoOutboxEvent.insert_or_get_by_idempotency_key(
        "widget.created", {"n": 1}, "widget-1", None, {}, write_connection, {"id": explicit_id}
    )
    second, second_is_new = await DemoOutboxEvent.insert_or_get_by_idempotency_key(
        "widget.created", {"n": 2}, "widget-1", None, {}, write_connection, {"id": explicit_id}
    )

    assert (first_is_new, second_is_new) == (True, False)
    assert second.id == first.id == explicit_id
    assert second.payload == {"n": 1}


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_same_key_without_explicit_id_is_new_only_once(db_outbox):
    write_connection = DemoOutboxEvent.get_connection(for_write=True)

    _, first_is_new = await DemoOutboxEvent.insert_or_get_by_idempotency_key(
        "widget.created", {}, "widget-1", None, {}, write_connection, {}
    )
    _, second_is_new = await DemoOutboxEvent.insert_or_get_by_idempotency_key(
        "widget.created", {}, "widget-1", None, {}, write_connection, {}
    )

    assert (first_is_new, second_is_new) == (True, False)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_unique_column_conflict_aborts_the_transaction_like_any_integrity_error(db_outbox):
    await UniqueReferenceOutboxEvent.enqueue("widget.created", {}, extra_field_values={"external_reference": "R1"})

    with pytest.raises(IntegrityError):
        async with Transactions.atomic(CONNECTION_ALIAS):
            await UniqueReferenceOutboxEvent.enqueue(
                "widget.created", {}, idempotency_key="k", extra_field_values={"external_reference": "R1"}
            )
    assert await UniqueReferenceOutboxEvent.objects.all().count() == 1


@pytest.mark.parametrize("field_name", sorted(RELAY_MANAGED_FIELD_NAMES))
@pytest.mark.asyncio
async def test_extra_field_values_cannot_set_relay_managed_fields(db_outbox, field_name):
    with pytest.raises(ValidationError, match=field_name):
        await DemoOutboxEvent.enqueue("widget.created", {}, extra_field_values={field_name: None})
    assert await DemoOutboxEvent.objects.all().count() == 0


@pytest.mark.asyncio
async def test_extra_field_values_can_set_the_primary_key(db_outbox):
    explicit_id = uuid.uuid4()

    event = await DemoOutboxEvent.enqueue("widget.created", {}, extra_field_values={"id": explicit_id})

    assert event.id == explicit_id
    assert await DemoOutboxEvent.objects.filter(id=explicit_id).exists()


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_key_unique_per_tenant_deduplicates_within_a_tenant_only(db_outbox):
    with Tenancy.scope(1):
        first = await TenantKeyOutboxEvent.enqueue("widget.created", {"n": 1}, idempotency_key="k")
        again = await TenantKeyOutboxEvent.enqueue("widget.created", {"n": 2}, idempotency_key="k")
    with Tenancy.scope(2):
        other_tenant = await TenantKeyOutboxEvent.enqueue("widget.created", {"n": 3}, idempotency_key="k")

    assert again.id == first.id
    assert other_tenant.id != first.id
    assert other_tenant.payload == {"n": 3}
    assert await TenantKeyOutboxEvent.objects.all_tenants().count() == 2


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_publish_under_a_scope_of_several_tenants_or_by_model(db_outbox):
    with Tenancy.scope(Tenancy.any_of(1, 2)):
        with pytest.raises(QueryError, match="has no single value to give it"):
            await TenantKeyOutboxEvent.enqueue("widget.created", {}, idempotency_key="k")
        first = await TenantKeyOutboxEvent.enqueue(
            "widget.created", {"n": 1}, idempotency_key="k", extra_field_values={"tenant_id": 1}
        )
        again = await TenantKeyOutboxEvent.enqueue(
            "widget.created", {"n": 2}, idempotency_key="k", extra_field_values={"tenant_id": 1}
        )
        second = await TenantKeyOutboxEvent.enqueue(
            "widget.created", {"n": 3}, idempotency_key="k", extra_field_values={"tenant_id": 2}
        )
        with pytest.raises(QueryError, match="tenant other than the active one"):
            await TenantKeyOutboxEvent.enqueue(
                "widget.created", {}, idempotency_key="k", extra_field_values={"tenant_id": 3}
            )
    assert again.id == first.id
    assert second.id != first.id
    with Tenancy.scope({TenantKeyOutboxEvent: 3}):
        third = await TenantKeyOutboxEvent.enqueue("widget.created", {"n": 4}, idempotency_key="k")
    assert third.tenant_id == 3
    with Tenancy.scope(Tenancy.ALL):
        # A model the scope leaves unlimited is published with its tenant named.
        fourth = await TenantKeyOutboxEvent.enqueue(
            "widget.created", {"n": 5}, idempotency_key="k", extra_field_values={"tenant_id": 4}
        )
    assert fourth.tenant_id == 4
    assert await TenantKeyOutboxEvent.objects.all_tenants().count() == 4


@pytest.mark.asyncio
async def test_key_without_any_unique_constraint_raises(db_outbox):
    with pytest.raises(ConfigurationError, match="isn't unique"):
        await NonUniqueKeyOutboxEvent.enqueue("widget.created", {}, idempotency_key="k")
    assert await NonUniqueKeyOutboxEvent.objects.all().count() == 0
