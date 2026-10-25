"""Drift through a shadow of the schema the models declare, read back as the database itself is: a change
of an index's expression, of a partial index's condition, of a CHECK, of a view's query or of a column's
default shows, a schema in sync shows nothing - whatever the database prints in words of its own - and
the shadow is gone afterwards, whatever happened."""

from __future__ import annotations

import datetime
import enum

import pytest

from hare import fields
from hare.ddl.constraints import CheckConstraint, ExclusionConstraint
from hare.ddl.enums import ExclusionConstraintUsing, TriggerEvent, TriggerTiming
from hare.ddl.indexes import Index, PartialIndex
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.ddl.schema_objects.trigger import Trigger
from hare.ddl.schema_objects.view import View
from hare.migrations.drift import detect_drift
from hare.migrations.drift.shadow.schema_comparison import SchemaComparison
from hare.migrations.state.model_state import ModelState
from hare.query.expressions import Q
from hare.query.functions import Lower
from tests.migrations.round_trips.test_round_trip_real_db import APP_LABEL, RoundTrip, build_live_state, build_model


def skip_unless_dialect(round_trip: RoundTrip, *dialects: str) -> None:
    if round_trip.dialect not in dialects:
        pytest.skip(f"the DDL changed by hand is of {', '.join(dialects)}")


async def get_operation_types(round_trip: RoundTrip, *models) -> list[str]:
    result = await detect_drift(round_trip.connection, build_live_state(*models), [APP_LABEL])
    return sorted(type(operation).__name__ for operation in result.operations)


@pytest.mark.asyncio
async def test_a_changed_expression_of_an_index_is_its_removal_and_addition(db_isolated_no_schema):
    round_trip = RoundTrip(db_isolated_no_schema.get_connection())
    skip_unless_dialect(round_trip, "sqlite", "postgresql")
    widget = build_model(
        "ShadowIndexWidget",
        "shadow_index_widget",
        {"name": fields.CharField(max_length=50)},
        {"indexes": [Index(Lower("name"), name="idx_shadow_lower_name")]},
    )
    await round_trip.migrate_to(widget)
    assert await get_operation_types(round_trip, widget) == []

    await round_trip.connection.execute_script(
        'DROP INDEX "idx_shadow_lower_name"; '
        'CREATE INDEX "idx_shadow_lower_name" ON "shadow_index_widget" (UPPER("name"))'
    )

    assert await get_operation_types(round_trip, widget) == ["AddIndex", "RemoveIndex"]


@pytest.mark.asyncio
async def test_a_changed_condition_of_a_partial_index_is_its_removal_and_addition(db_isolated_no_schema):
    round_trip = RoundTrip(db_isolated_no_schema.get_connection())
    skip_unless_dialect(round_trip, "sqlite", "postgresql")
    widget = build_model(
        "ShadowPartialWidget",
        "shadow_partial_widget",
        {"name": fields.CharField(max_length=50), "rank": fields.IntField()},
        {"indexes": [PartialIndex(fields=("name",), condition=Q(rank__gt=0), name="idx_shadow_ranked_name")]},
    )
    await round_trip.migrate_to(widget)
    assert await get_operation_types(round_trip, widget) == []

    await round_trip.connection.execute_script(
        'DROP INDEX "idx_shadow_ranked_name"; '
        'CREATE INDEX "idx_shadow_ranked_name" ON "shadow_partial_widget" ("name") WHERE "rank" > 1'
    )

    assert await get_operation_types(round_trip, widget) == ["AddIndex", "RemoveIndex"]


@pytest.mark.asyncio
async def test_a_changed_check_constraint_is_its_removal_and_addition(db_isolated_no_schema):
    round_trip = RoundTrip(db_isolated_no_schema.get_connection())
    skip_unless_dialect(round_trip, "postgresql")
    widget = build_model(
        "ShadowCheckWidget",
        "shadow_check_widget",
        {"low": fields.IntField(), "high": fields.IntField()},
        {"constraints": [CheckConstraint(check=Q(high__gte=0), name="ck_shadow_high")]},
    )
    await round_trip.migrate_to(widget)
    assert await get_operation_types(round_trip, widget) == []

    await round_trip.connection.execute_script(
        'ALTER TABLE "shadow_check_widget" DROP CONSTRAINT "ck_shadow_high"; '
        'ALTER TABLE "shadow_check_widget" ADD CONSTRAINT "ck_shadow_high" CHECK ("high" >= "low")'
    )

    assert await get_operation_types(round_trip, widget) == ["AddConstraint", "RemoveConstraint"]


@pytest.mark.asyncio
async def test_a_changed_query_of_a_view_is_its_alteration(db_isolated_no_schema):
    round_trip = RoundTrip(db_isolated_no_schema.get_connection())
    skip_unless_dialect(round_trip, "postgresql")
    widget = build_model(
        "ShadowViewWidget",
        "shadow_view_widget",
        {"rank": fields.IntField()},
        {
            "views": [
                View(
                    "shadow_ranked_widgets",
                    RawSQLTerm('SELECT "id", "rank" FROM "shadow_view_widget" WHERE "rank" > 0'),
                )
            ]
        },
    )
    await round_trip.migrate_to(widget)
    assert await get_operation_types(round_trip, widget) == []

    await round_trip.connection.execute_script(
        'DROP VIEW "shadow_ranked_widgets"; '
        'CREATE VIEW "shadow_ranked_widgets" AS SELECT "id", "rank" FROM "shadow_view_widget" WHERE "rank" > 1'
    )

    assert await get_operation_types(round_trip, widget) == ["AlterView"]


@pytest.mark.asyncio
async def test_a_changed_date_default_of_a_column_is_its_alteration(db_isolated_no_schema):
    round_trip = RoundTrip(db_isolated_no_schema.get_connection())
    skip_unless_dialect(round_trip, "postgresql")
    widget = build_model(
        "ShadowDefaultWidget",
        "shadow_default_widget",
        {"since": fields.DateField(db_default=datetime.date(2020, 1, 1))},
    )
    await round_trip.migrate_to(widget)
    assert await get_operation_types(round_trip, widget) == []

    await round_trip.connection.execute_script(
        'ALTER TABLE "shadow_default_widget" ALTER COLUMN "since" SET DEFAULT \'2021-01-01\''
    )

    assert await get_operation_types(round_trip, widget) == ["AlterField"]


@pytest.mark.asyncio
async def test_a_hash_exclusion_constraint_in_sync_is_no_drift(db_isolated_no_schema):
    round_trip = RoundTrip(db_isolated_no_schema.get_connection())
    skip_unless_dialect(round_trip, "postgresql")
    widget = build_model(
        "ShadowHashWidget",
        "shadow_hash_widget",
        {"resource": fields.IntField()},
        {
            "constraints": [
                ExclusionConstraint(
                    name="excl_shadow_resource",
                    expressions=(("resource", "="),),
                    using=ExclusionConstraintUsing.HASH,
                )
            ]
        },
    )
    await round_trip.migrate_to(widget)

    assert await get_operation_types(round_trip, widget) == []


class ShadowColor(enum.Enum):
    RED = "red"
    GREEN = "green"


@pytest.mark.asyncio
async def test_models_of_several_schemas_with_an_enum_type_in_sync_are_no_drift(db_isolated_no_schema):
    from hare.dialects.postgresql.fields import NativeEnumField

    round_trip = RoundTrip(db_isolated_no_schema.get_connection())
    skip_unless_dialect(round_trip, "postgresql")
    painted = build_model(
        "ShadowPainted",
        "shadow_painted",
        {"color": NativeEnumField(ShadowColor, type_name="shadow_color")},
    )
    archived = build_model(
        "ShadowArchived",
        "shadow_archived",
        {"color": NativeEnumField(ShadowColor, type_name="shadow_color")},
        {"schema": "shadow_archive"},
    )
    await round_trip.migrate_to(painted, archived)
    assert await get_operation_types(round_trip, painted, archived) == []

    await round_trip.connection.execute_script("ALTER TYPE \"shadow_color\" ADD VALUE 'blue'")

    assert await get_operation_types(round_trip, painted, archived) == ["AlterEnumType"]


@pytest.mark.asyncio
async def test_the_shadow_is_removed_when_the_comparison_fails(db_isolated_no_schema, monkeypatch):
    round_trip = RoundTrip(db_isolated_no_schema.get_connection())
    skip_unless_dialect(round_trip, "postgresql")
    widget = build_model("ShadowFailWidget", "shadow_fail_widget", {"rank": fields.IntField()})
    await round_trip.migrate_to(widget)

    def fail(*arguments, **keyword_arguments):
        raise RuntimeError("the comparison failed")

    monkeypatch.setattr(SchemaComparison, "compare_model", fail)
    with pytest.raises(RuntimeError, match="the comparison failed"):
        await detect_drift(round_trip.connection, build_live_state(widget), [APP_LABEL])

    rows = await round_trip.connection.execute_dicts(
        "SELECT nspname FROM pg_namespace WHERE nspname LIKE 'hare\\_shadow\\_%'"
    )
    assert rows == []


@pytest.mark.asyncio
async def test_an_unmanaged_model_over_a_view_is_a_bare_table_in_the_shadow(db_isolated_no_schema):
    """The shadow made the table of an unmanaged model over a view and put its INSTEAD OF trigger on it -
    PostgreSQL takes none on a table, and drift failed."""
    round_trip = RoundTrip(db_isolated_no_schema.get_connection())
    skip_unless_dialect(round_trip, "postgresql")
    row = build_model("ShadowViewRow", "shadow_view_row", {"val": fields.IntField()})
    await round_trip.migrate_to(row)
    await round_trip.connection.execute_script(
        'CREATE VIEW "shadow_view_rows" AS SELECT "id", "val" FROM "shadow_view_row"'
    )
    trigger = Trigger(
        name="shadow_view_rows_insert",
        on=TriggerEvent.INSERT,
        timing=TriggerTiming.INSTEAD_OF,
        body=RawSQLTerm('INSERT INTO "shadow_view_row" ("id", "val") VALUES (NEW."id", NEW."val"); RETURN NEW;'),
    )
    over_view = build_model(
        "ShadowViewRows", "shadow_view_rows", {"val": fields.IntField()}, {"managed": False, "triggers": [trigger]}
    )

    result = await detect_drift(
        round_trip.connection,
        build_live_state(row),
        [APP_LABEL],
        unmanaged_model_states=[ModelState.make_from_model(APP_LABEL, over_view)],
    )

    assert result.operations == []
    assert result.untracked_tables == []
