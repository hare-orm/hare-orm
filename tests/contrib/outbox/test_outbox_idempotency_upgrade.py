"""Upgrade path for an existing OutboxEvent subclass: the autodetector turns the new
idempotency_key field into one nullable, unique AddField that applies cleanly to a populated
table on every backend, and NULL keys never conflict afterwards."""

from typing import Any
from uuid import uuid4

import pytest

from hare import fields
from hare.contrib.outbox.models import OutboxEvent
from hare.contrib.test import requires_features
from hare.ddl.indexes import Index, PartialIndex
from hare.exceptions import IntegrityError
from hare.migrations.operations import AddField
from hare.models import Model
from hare.query.expressions import Q
from tests.migrations.test_round_trip_real_db import APP_LABEL, RoundTrip

TABLE = "outbox_upgrade_event"
MODEL_NAME = "OutboxUpgradeEvent"


def build_pre_upgrade_model() -> type[Model]:
    """OutboxEvent's field set as it was before idempotency_key existed."""
    attributes: dict[str, Any] = {
        "id": fields.UUIDField(primary_key=True, default=uuid4),
        "topic": fields.CharField(max_length=255, db_index=True),
        "payload": fields.JSONField(),
        "created_at": fields.DatetimeField(auto_now_add=True, db_index=True),
        "published_at": fields.DatetimeField(
            null=True, description=OutboxEvent._meta.fields_map["published_at"].description
        ),
        "attempts": fields.IntField(default=0),
        "last_error": fields.TextField(null=True),
        "_no_comments": True,
        "Meta": type(
            "Meta",
            (),
            {
                "table": TABLE,
                "app": APP_LABEL,
                "indexes": (
                    Index(fields=("topic", "created_at")),
                    PartialIndex(fields=("published_at",), condition=Q(published_at__isnull=True)),
                ),
            },
        ),
    }
    return type(MODEL_NAME, (Model,), attributes)


def build_current_model() -> type[Model]:
    meta = type("Meta", (OutboxEvent.Meta,), {"table": TABLE, "app": APP_LABEL})
    return type(MODEL_NAME, (OutboxEvent,), {"_no_comments": True, "Meta": meta})


async def insert_row(round_trip: RoundTrip, idempotency_key: str | None) -> None:
    placeholders = ("?", "?", "?") if round_trip.dialect == "sqlite" else ("$1", "$2", "$3")
    await round_trip.connection.execute(
        f"INSERT INTO {TABLE} (id, topic, payload, created_at, attempts, idempotency_key) "
        f"VALUES ({placeholders[0]}, {placeholders[1]}, '{{}}', CURRENT_TIMESTAMP, 0, {placeholders[2]})",
        [str(uuid4()), "topic", idempotency_key],
    )


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_autodetected_add_field_upgrades_an_existing_outbox_table(db_isolated_no_schema):
    round_trip = RoundTrip(db_isolated_no_schema.db())
    await round_trip.migrate_to(build_pre_upgrade_model())

    current_model = build_current_model()
    operations = await round_trip.migrate_to(current_model)

    assert len(operations) == 1
    operation = operations[0]
    assert isinstance(operation, AddField)
    assert operation.name == "idempotency_key"
    assert operation.field.null is True
    assert operation.field.unique is True
    assert round_trip.get_pending_operations(current_model) == []

    await insert_row(round_trip, None)
    await insert_row(round_trip, None)
    await insert_row(round_trip, "key-1")
    with pytest.raises(IntegrityError):
        await insert_row(round_trip, "key-1")
