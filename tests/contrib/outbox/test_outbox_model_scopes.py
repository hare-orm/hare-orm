"""An explicit event tenant fills only the event model's missing scope."""

import sys
from types import ModuleType

import pytest
import pytest_asyncio

from hare import Model, fields
from hare.contrib.outbox import OutboxEvent, OutboxMessage
from hare.contrib.test import requires_features
from hare.core.hare_context import HareContext
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.exceptions import QueryError
from hare.models.tenancy.tenancy import Tenancy
from tests.utils.database_under_test import DatabaseUnderTest


@pytest_asyncio.fixture(params=("scalar", "relation"))
async def keyed_event_model(request, monkeypatch):
    class NormalizedKeyField(fields.CharField):
        def to_db_value(self, value, instance):
            value = super().to_db_value(value, instance)
            return None if value is None else value.strip().lower()

    class Company(Model):
        id = fields.IntField(primary_key=True, generated=False, source_field="company_number")

    class KeyedEvent(OutboxEvent):
        company = (
            fields.ForeignKeyField("models.Company", related_name=False, source_field="event_company")
            if request.param == "relation"
            else fields.IntField(source_field="event_company")
        )
        idempotency_key = NormalizedKeyField(max_length=255, null=True)

        class Meta:
            tenant_field = "company"
            constraints = (
                UniqueConstraint(
                    fields=("company_id" if request.param == "relation" else "company", "idempotency_key"),
                    name="company_event_key",
                ),
            )

    module = ModuleType("tests._outbox_keyed_scopes")
    module.__models__ = [Company, KeyedEvent]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    async with DatabaseUnderTest.open_model_context([module.__name__]):
        await Company.objects.create(id=1)
        yield KeyedEvent, "company_id" if request.param == "relation" else "company"


@pytest.mark.asyncio
@requires_features(supports_insert_conflicts=True)
@pytest.mark.parametrize("operation", ("single", "many"))
@pytest.mark.parametrize("explicit_tenant", (False, True))
async def test_idempotency_retains_the_effective_tenant_after_a_skipped_insert(
    keyed_event_model, operation, explicit_tenant
):
    event_model, attribute = keyed_event_model
    values = {attribute: 1} if explicit_tenant else {}
    with Tenancy.scope(1):
        first = await event_model.enqueue("original", {"value": 1}, idempotency_key="same", extra_field_values=values)
        if operation == "single":
            events = [
                await event_model.enqueue("ignored", {"value": 2}, idempotency_key="same", extra_field_values=values)
            ]
        else:
            events = await event_model.enqueue_many(
                [
                    OutboxMessage("ignored", {"value": 2}, idempotency_key="same", extra_field_values=values),
                    OutboxMessage("ignored", {"value": 3}, idempotency_key="same", extra_field_values=values),
                ]
            )
        assert all(event.id == first.id and event.payload == {"value": 1} for event in events)
        assert all(getattr(event, attribute) == 1 for event in events)
        assert await event_model.objects.count() == 1
        # Ordinary bulk inputs still regain their original state after a skipped insert.
        candidate = event_model(topic="ignored", payload={}, idempotency_key="same")
        assert (
            await event_model.objects.bulk_create(
                [candidate], ignore_conflicts=True, on_conflict=[attribute, "idempotency_key"]
            )
            == []
        )
        assert getattr(candidate, attribute) is None
        assert not candidate._saved_in_db


@pytest.mark.asyncio
@requires_features(supports_insert_conflicts=True)
async def test_many_idempotency_uses_database_equality_and_preserves_input_order(keyed_event_model):
    event_model, attribute = keyed_event_model
    values = {attribute: 1}
    with Tenancy.scope(1):
        first = await event_model.enqueue("original", {"value": 1}, idempotency_key="first", extra_field_values=values)
        events = await event_model.enqueue_many(
            [
                OutboxMessage("ignored", {"value": 2}, idempotency_key=" FIRST ", extra_field_values=values),
                OutboxMessage("second", {"value": 3}, idempotency_key=" SECOND ", extra_field_values=values),
                OutboxMessage("ignored", {"value": 4}, idempotency_key="second", extra_field_values=values),
                OutboxMessage("ignored", {"value": 5}, idempotency_key="First", extra_field_values=values),
            ]
        )
        assert [event.payload for event in events] == [{"value": 1}, {"value": 3}, {"value": 3}, {"value": 1}]
        assert events[0].id == events[3].id == first.id
        assert events[1] is events[2]
        assert await event_model.objects.count() == 2


@pytest_asyncio.fixture(params=(False, True), ids=("single-key", "composite-key"))
async def event_models(request, monkeypatch):
    class Recipient(Model):
        number = fields.IntField(primary_key=not request.param, generated=False)
        if request.param:
            revision = fields.IntField()
            pk = fields.CompositePrimaryKey("number", "revision")
        tenant = fields.IntField()

        class Meta:
            tenant_field = "tenant"

    class Event(OutboxEvent):
        tenant = fields.IntField()
        recipient = fields.ForeignKeyField("models.Recipient", related_name=False)

        class Meta:
            tenant_field = "tenant"

    module = ModuleType("tests._outbox_model_scopes")
    module.__models__ = [Recipient, Event]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    async with DatabaseUnderTest.open_model_context([module.__name__]):
        first = await Recipient.objects.create(number=1, **({"revision": 1} if request.param else {}), tenant=99)
        second = await Recipient.objects.create(number=2, **({"revision": 1} if request.param else {}), tenant=100)
        yield Event, first, second


@pytest.mark.asyncio
@requires_features(supports_insert_conflicts=True)
@pytest.mark.parametrize("operation", ("single-keyed", "many-keyed", "many-unkeyed"))
@pytest.mark.parametrize("scope", ("none", "recipient", "all-recipients"))
async def test_explicit_event_tenants_preserve_other_models_scopes(event_models, operation, scope):
    event_model, recipient, other = event_models
    recipient_model = type(recipient)
    active_scope = None if scope == "none" else {recipient_model: 99 if scope == "recipient" else Tenancy.ALL}
    with Tenancy.scope(active_scope):
        previous_scope = Tenancy.current.get()
        for _ in range(2):
            values = {"tenant": 42, "recipient": recipient}
            if operation == "single-keyed":
                event = await event_model.enqueue("created", {}, idempotency_key="first", extra_field_values=values)
            else:
                [event] = await event_model.enqueue_many(
                    [
                        OutboxMessage(
                            "created",
                            {},
                            idempotency_key="first" if operation == "many-keyed" else None,
                            extra_field_values=values,
                        )
                    ]
                )
            assert event.tenant == 42
            relation = HareContext.require_current().apps.get_binding(event_model).fields_map["recipient"]
            assert tuple(getattr(event, name) for name in relation.source_fields) == tuple(
                getattr(recipient, name) for name in relation.to_field_names
            )
            assert Tenancy.current.get() is previous_scope
            assert Tenancy.get_scope(HareContext.require_current().apps.get_binding(event_model)) is None
        if scope == "recipient":
            with pytest.raises(QueryError):
                await event_model.enqueue(
                    "other", {}, idempotency_key="other", extra_field_values={"tenant": 42, "recipient": other}
                )
            assert Tenancy.current.get() is previous_scope
