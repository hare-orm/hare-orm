"""Drift through a shadow of the schema the models declare, read back as the database itself is: a change
of an index's expression, of a partial index's condition, of a CHECK, of a view's query or of a column's
default shows, a schema in sync shows nothing - whatever the database prints in words of its own - and
the shadow is gone afterwards, whatever happened."""

from __future__ import annotations

import datetime
import enum

import pytest

from hare import Hare, fields
from hare.ddl.constraints import CheckConstraint, ExclusionConstraint
from hare.ddl.enums import ExclusionConstraintUsing, TriggerEvent, TriggerTiming
from hare.ddl.indexes import Index, PartialIndex
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.ddl.schema_objects.database_sequence import DatabaseSequence
from hare.ddl.schema_objects.trigger import Trigger
from hare.ddl.schema_objects.view import View
from hare.migrations.drift import detect_drift
from hare.migrations.drift.shadow.schema_comparison import SchemaComparison
from hare.migrations.operations import SeparateDatabaseAndState
from hare.migrations.state.model_state import ModelState
from hare.migrations.writer import MigrationWriter
from hare.query.expressions import Q
from hare.query.functions import Lower
from tests.migrations.round_trips.test_round_trip_real_db import APP_LABEL, RoundTrip, build_live_state, build_model
from tests.utils.migration_source import MigrationSource


def skip_unless_dialect(round_trip: RoundTrip, *dialects: str) -> None:
    if round_trip.dialect not in dialects:
        pytest.skip(f"the DDL changed by hand is of {', '.join(dialects)}")


async def get_operation_types(round_trip: RoundTrip, *models) -> list[str]:
    result = await round_trip.get_drift(build_live_state(*models))
    return sorted(
        type(operation).__name__
        for operation in result.operations
        if not isinstance(operation, SeparateDatabaseAndState)
    )


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
async def test_repair_migration_starts_from_the_observed_table(db_isolated_no_schema):
    round_trip = RoundTrip(db_isolated_no_schema.get_connection())
    skip_unless_dialect(round_trip, "sqlite", "postgresql")
    expected = build_model(
        "Invoice", "invoice", {"amount": fields.IntField(db_default=17, db_index=True), "memo": fields.IntField()}
    )
    observed = build_model(
        "Invoice",
        "invoice",
        {"amount": fields.IntField(db_default=19), "memo": fields.TextField()},
        {
            "indexes": [Index(fields=("memo",), name="extra_memo")],
            "constraints": [CheckConstraint(Q(amount__gte=0), name="extra_amount")],
        },
    )
    await round_trip.migrate_to(expected)
    tracked_state = round_trip.tracked_state
    await round_trip.migrate_to(observed)
    await round_trip.connection.execute_script("INSERT INTO invoice (id, memo) VALUES (1, '42')")
    drift = await round_trip.get_drift(tracked_state)
    operations = drift.operations_by_app[APP_LABEL]
    assert isinstance(operations[0], SeparateDatabaseAndState)
    assert drift.mismatched_objects == []

    source = MigrationWriter("repair", APP_LABEL, operations).as_string()
    repair = MigrationSource.load(source, "repair", APP_LABEL, filename="<drift repair>")
    await repair.apply(tracked_state, schema_editor=round_trip.editor)
    assert not (await round_trip.get_drift(tracked_state)).has_drift
    await round_trip.connection.execute_script("INSERT INTO invoice (id, memo) VALUES (2, 43)")
    assert await round_trip.connection.execute_dicts("SELECT amount, memo FROM invoice ORDER BY id") == [
        {"amount": 19, "memo": 42},
        {"amount": 17, "memo": 43},
    ]

    await repair.unapply(tracked_state, schema_editor=round_trip.editor)
    assert (await round_trip.get_drift(tracked_state)).has_drift


@pytest.mark.asyncio
async def test_drift_reports_a_repair_that_would_discard_an_untracked_column(db_isolated_no_schema):
    round_trip = RoundTrip(db_isolated_no_schema.get_connection())
    skip_unless_dialect(round_trip, "sqlite", "postgresql")
    expected = build_model("Invoice", "invoice", {"amount": fields.IntField(db_default=17)})
    observed = build_model(
        "Invoice", "invoice", {"amount": fields.IntField(db_default=19), "memo": fields.TextField(null=True)}
    )
    await round_trip.migrate_to(observed)
    await round_trip.connection.execute_script("INSERT INTO invoice (id, memo) VALUES (1, 'keep')")

    result = await round_trip.get_drift(build_live_state(expected))
    assert result.operations_by_app == {}
    assert result.untracked_columns == [(APP_LABEL, "Invoice", "invoice", "memo")]
    assert "rebuild" in result.mismatched_objects[0].detail
    assert await round_trip.connection.execute_dicts("SELECT amount, memo FROM invoice") == [
        {"amount": 19, "memo": "keep"}
    ]


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
@pytest.mark.parametrize("explicit_default_schema", [False, True])
async def test_models_of_several_schemas_with_an_enum_type_in_sync_are_no_drift(
    db_isolated_no_schema, explicit_default_schema
):
    from hare.dialects.postgresql.fields import NativeEnumField
    from hare.inspectdb.introspection.database_catalog import DatabaseCatalog

    round_trip = RoundTrip(db_isolated_no_schema.get_connection())
    skip_unless_dialect(round_trip, "postgresql")
    type_schema = await DatabaseCatalog.get_default_schema(round_trip.connection) if explicit_default_schema else None
    painted = build_model(
        "ShadowPainted",
        "shadow_painted",
        {"color": NativeEnumField(ShadowColor, type_name="shadow_color", type_schema=type_schema)},
    )
    archived = build_model(
        "ShadowArchived",
        "shadow_archived",
        {"color": NativeEnumField(ShadowColor, type_name="shadow_color", type_schema=type_schema)},
        {"schema": "shadow_archive"},
    )
    await round_trip.migrate_to(painted, archived)
    assert await get_operation_types(round_trip, painted, archived) == []

    await round_trip.connection.execute_script("ALTER TYPE \"shadow_color\" ADD VALUE 'blue'")

    assert await get_operation_types(round_trip, painted, archived) == ["AlterEnumType"]


@pytest.mark.asyncio
async def test_drift_finds_unattached_sequences_by_schema_and_name(db_isolated_no_schema):
    round_trip = RoundTrip(db_isolated_no_schema.get_connection())
    skip_unless_dialect(round_trip, "postgresql")
    active = build_model("Active", "entry", {}, {"sequences": [DatabaseSequence("entry_numbers", increment=4)]})
    archived = build_model(
        "Archived", "entry", {}, {"schema": "archive", "sequences": [DatabaseSequence("entry_numbers", increment=4)]}
    )
    await round_trip.migrate_to(active, archived)
    assert await get_operation_types(round_trip, active, archived) == []

    await round_trip.connection.execute_script('ALTER SEQUENCE "entry_numbers" INCREMENT BY 8')
    await round_trip.connection.execute_script('DROP SEQUENCE "archive"."entry_numbers"')
    drift = await round_trip.get_drift(build_live_state(active, archived))
    assert sorted(
        (type(op).__name__, op.model_name) for op in drift.operations if not isinstance(op, SeparateDatabaseAndState)
    ) == [
        ("AddSequence", "Archived"),
        ("AlterSequence", "Active"),
    ]


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
        await round_trip.get_drift(build_live_state(widget))

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
        unmanaged_model_states=[ModelState.make_from_declaration(APP_LABEL, over_view)],
    )

    assert result.operations == []
    assert result.untracked_tables == []


@pytest.mark.asyncio
async def test_an_unmanaged_models_generated_non_pk_column_is_not_created_in_the_shadow(db_isolated_no_schema):
    """An unmanaged model can describe an IDENTITY column outside its primary key; hare can't create that
    column in the shadow standing in for its table."""
    round_trip = RoundTrip(db_isolated_no_schema.get_connection())
    skip_unless_dialect(round_trip, "postgresql")
    row = build_model("ShadowIdentityRow", "shadow_identity_row", {"val": fields.IntField()})
    await round_trip.migrate_to(row)
    await round_trip.connection.execute_script(
        'CREATE TABLE "shadow_unmanaged_identity" ('
        '"id" INT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, '
        '"external_sequence" INT GENERATED BY DEFAULT AS IDENTITY)'
    )
    unmanaged = build_model(
        "ShadowUnmanagedIdentity",
        "shadow_unmanaged_identity",
        {"external_sequence": fields.IntField(generated=True)},
        {"managed": False},
    )

    result = await detect_drift(
        round_trip.connection,
        build_live_state(row),
        [APP_LABEL],
        unmanaged_model_states=[ModelState.make_from_declaration(APP_LABEL, unmanaged)],
    )

    assert result.operations == []
    assert result.untracked_tables == []


@pytest.mark.asyncio
async def test_a_relation_can_target_an_unmanaged_identity_column_in_the_shadow(db_isolated_no_schema):
    round_trip = RoundTrip(db_isolated_no_schema.get_connection())
    skip_unless_dialect(round_trip, "postgresql")
    await round_trip.connection.execute_script(
        'CREATE TABLE "shadow_external_identity" ('
        '"id" SERIAL PRIMARY KEY, "sequence" INT GENERATED BY DEFAULT AS IDENTITY UNIQUE); '
        'CREATE TABLE "shadow_identity_reference" ('
        '"id" SERIAL PRIMARY KEY, "external_id" INT NOT NULL '
        'REFERENCES "shadow_external_identity" ("sequence") ON DELETE CASCADE)'
    )
    external = build_model(
        "ShadowExternalIdentity",
        "shadow_external_identity",
        {"sequence": fields.IntField(generated=True, unique=True)},
        {"managed": False},
    )
    reference = build_model(
        "ShadowIdentityReference",
        "shadow_identity_reference",
        {"external": fields.ForeignKeyField("models.ShadowExternalIdentity", to_field="sequence", db_index=False)},
    )
    Hare.register_live_models(
        [external, reference], app_label=APP_LABEL, connection_alias=round_trip.connection.connection_alias
    )
    external_state = ModelState.make_from_declaration(APP_LABEL, external)
    result = await detect_drift(
        round_trip.connection,
        build_live_state(reference),
        [APP_LABEL],
        unmanaged_model_states=[external_state],
    )
    assert result.operations == []
    assert result.untracked_tables == []
    assert external_state.fields["sequence"].generated is True
