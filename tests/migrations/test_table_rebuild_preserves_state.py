"""A column change SQLite applies by rebuilding the table - what the rebuild must carry over or
convert, checked on every database the same operations run on."""

from __future__ import annotations

import datetime

import pytest

from hare.contrib.test import requires_features
from hare.ddl import RawSQLTerm
from hare.ddl.constraints import UniqueConstraint
from hare.exceptions import IntegrityError
from hare.fields.data.boolean import BooleanField
from hare.fields.data.numeric import BigIntField, DecimalField, IntField
from hare.fields.data.temporal import DateField, DatetimeField
from hare.fields.data.text import CharField
from hare.fields.generated import GeneratedField
from hare.migrations.drift import detect_drift
from hare.migrations.operations import AddField, AddIndex
from tests.migrations.test_round_trip_real_db import APP_LABEL, RoundTrip, build_live_state, build_model


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "added_field",
    [
        pytest.param(CharField(max_length=10, db_index=True, default="x"), id="not_null_with_python_default"),
        pytest.param(
            GeneratedField("price * 2", output_field=IntField(), db_index=True),
            id="generated_field",
        ),
    ],
)
async def test_add_indexed_field_through_a_rebuild_creates_its_index_once(db_isolated, added_field):
    """The rebuild of an AddField used to create the new column's own index, then the AddIndex
    written after it failed with "index ... already exists"."""
    round_trip = RoundTrip(db_isolated.db())
    await round_trip.migrate_to(build_model("Item", "rebuild_item", {"price": IntField()}))
    await round_trip.connection.execute_script("INSERT INTO rebuild_item (price) VALUES (3)")
    widened = build_model("Item", "rebuild_item", {"price": IntField(), "extra": added_field})

    operations = await round_trip.migrate_to(widened)

    assert [type(operation) for operation in operations] == [AddField, AddIndex]
    index_names = [name for name in await round_trip.get_index_definitions("rebuild_item") if "extra" in name]
    assert len(index_names) == 1
    assert round_trip.get_pending_operations(widened) == []


@pytest.mark.asyncio
async def test_type_change_converts_stored_values_to_the_new_format(db_isolated):
    """A rebuild copied every value as it was - a datetime text in a DATE column, a date in a
    TIMESTAMP one, 5 in a boolean one - so filters on the new type matched nothing."""
    round_trip = RoundTrip(db_isolated.db())
    await round_trip.migrate_to(
        build_model(
            "Typed",
            "rebuild_typed",
            {"moment": DatetimeField(), "day": DateField(), "count": IntField(), "number": IntField()},
        )
    )
    await round_trip.connection.execute(
        "INSERT INTO rebuild_typed (moment, day, count, number) VALUES ($1, $2, 5, 7)"
        if round_trip.dialect == "postgresql"
        else "INSERT INTO rebuild_typed (moment, day, count, number) VALUES (?, ?, 5, 7)",
        [datetime.datetime(2024, 5, 6, 7, 8, 9, tzinfo=datetime.UTC), datetime.date(2024, 1, 2)],
    )
    changed = build_model(
        "Typed",
        "rebuild_typed",
        {
            "moment": DateField(),
            "day": DatetimeField(),
            "count": BooleanField(),
            "number": DecimalField(max_digits=10, decimal_places=2),
        },
    )

    await round_trip.migrate_to(changed)

    [row] = await round_trip.connection.execute_dicts("SELECT moment, day, count, number FROM rebuild_typed")
    if round_trip.dialect == "sqlite":
        assert row["moment"] == "2024-05-06"
        assert row["day"].startswith("2024-01-02 00:00:00")
        assert row["count"] == 1
        assert row["number"] == "7.00"
    else:
        assert row["moment"] == datetime.date(2024, 5, 6)
        assert row["count"] is True
        assert str(row["number"]) == "7.00"


@pytest.mark.asyncio
async def test_rebuild_of_a_table_a_view_reads_keeps_the_view_working(db_isolated):
    """The rename of the rebuilt table re-validated every view, and failed on one naming the
    table while it was briefly missing."""
    round_trip = RoundTrip(db_isolated.db())
    await round_trip.migrate_to(build_model("Viewed", "rebuild_viewed", {"quantity": IntField()}))
    await round_trip.connection.execute_script(
        "INSERT INTO rebuild_viewed (quantity) VALUES (4); "
        "CREATE VIEW rebuild_viewed_view AS SELECT quantity FROM rebuild_viewed"
    )
    changed = build_model("Viewed", "rebuild_viewed", {"quantity": BigIntField()})

    if round_trip.dialect == "postgresql":
        # Postgres itself refuses to change the type of a column a view reads.
        await round_trip.connection.execute_script("DROP VIEW rebuild_viewed_view")
        await round_trip.migrate_to(changed)
        return
    await round_trip.migrate_to(changed)

    assert await round_trip.connection.execute_dicts("SELECT quantity FROM rebuild_viewed_view") == [{"quantity": 4}]


@pytest.mark.asyncio
async def test_rebuild_keeps_the_autoincrement_counter(db_isolated):
    """A rebuilt table started its AUTOINCREMENT counter from the highest id left in it, so the
    id of a deleted newest row was issued again."""
    round_trip = RoundTrip(db_isolated.db())
    await round_trip.migrate_to(build_model("Counted", "rebuild_counted", {"quantity": IntField()}))
    await round_trip.connection.execute_script(
        "INSERT INTO rebuild_counted (quantity) VALUES (1); "
        "INSERT INTO rebuild_counted (quantity) VALUES (2); "
        "INSERT INTO rebuild_counted (quantity) VALUES (3); "
        "DELETE FROM rebuild_counted WHERE quantity = 3"
    )

    await round_trip.migrate_to(build_model("Counted", "rebuild_counted", {"quantity": BigIntField()}))
    await round_trip.connection.execute_script("INSERT INTO rebuild_counted (quantity) VALUES (4)")

    [row] = await round_trip.connection.execute_dicts("SELECT id FROM rebuild_counted WHERE quantity = 4")
    assert row["id"] == 4


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_partial_unique_constraint_survives_rebuilds_and_matches_drift(db_isolated):
    """SQLite has partial indexes - a UniqueConstraint(condition=...) is created, kept by every
    rebuild of its table, enforced, and seen by drift as the declared constraint."""
    round_trip = RoundTrip(db_isolated.db())
    meta_options = {
        "constraints": [
            UniqueConstraint(fields=("name",), name="uq_partial_open_name", condition=RawSQLTerm("status = 'open'")),
            UniqueConstraint(fields=("name", "quantity"), condition=RawSQLTerm("quantity > 0")),
        ]
    }
    columns = {"name": CharField(max_length=20), "status": CharField(max_length=10), "quantity": IntField()}
    await round_trip.migrate_to(build_model("Partial", "rebuild_partial", columns, meta_options))
    widened_columns = {**columns, "quantity": BigIntField()}
    widened = build_model("Partial", "rebuild_partial", widened_columns, meta_options)

    await round_trip.migrate_to(widened)

    index_definitions = await round_trip.get_index_definitions("rebuild_partial")
    assert "WHERE" in index_definitions["uq_partial_open_name"].upper()
    assert sum("WHERE" in definition.upper() for definition in index_definitions.values()) == 2
    assert round_trip.get_pending_operations(widened) == []
    assert (await detect_drift(round_trip.connection, build_live_state(widened), [APP_LABEL])).operations == []
    await round_trip.connection.execute_script(
        "INSERT INTO rebuild_partial (name, status, quantity) VALUES ('a', 'open', 0); "
        "INSERT INTO rebuild_partial (name, status, quantity) VALUES ('a', 'closed', 0)"
    )
    with pytest.raises(IntegrityError):
        await round_trip.connection.execute_script(
            "INSERT INTO rebuild_partial (name, status, quantity) VALUES ('a', 'open', 0)"
        )
