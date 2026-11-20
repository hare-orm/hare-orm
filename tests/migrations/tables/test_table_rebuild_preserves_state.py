"""A column change SQLite applies by rebuilding the table - what the rebuild must carry over or
convert, checked on every database the same operations run on."""

from __future__ import annotations

import datetime

import pytest

from hare.contrib.test import requires_features
from hare.ddl import RawSQLTerm
from hare.ddl.constraints import CheckConstraint, UniqueConstraint
from hare.exceptions import IntegrityError
from hare.fields.data.boolean_field import BooleanField
from hare.fields.data.numeric import BigIntField, DecimalField, FloatField, IntField
from hare.fields.data.temporal import DateField, DatetimeField
from hare.fields.data.text import CharField
from hare.fields.generated_field import GeneratedField
from hare.migrations.operations import AddConstraint, AddField
from tests.migrations.round_trips.test_round_trip_real_db import RoundTrip, build_live_state, build_model
from tests.utils.database_under_test import DatabaseUnderTest


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "added_field",
    [
        pytest.param(CharField(max_length=10, db_index=True, default="x"), id="not_null_with_python_default"),
        pytest.param(
            GeneratedField(RawSQLTerm("price * 2"), output_field=IntField(), db_index=True),
            id="generated_field",
        ),
    ],
)
async def test_add_indexed_field_through_a_rebuild_creates_its_index_once(db_isolated_no_schema, added_field):
    """The rebuild of an AddField creates the new column's own index by its flag - once: the AddField
    makes no second one after the rebuild."""
    round_trip = RoundTrip(db_isolated_no_schema.get_connection())
    await round_trip.migrate_to(build_model("Item", "rebuild_item", {"price": IntField()}))
    await round_trip.connection.execute_script("INSERT INTO rebuild_item (price) VALUES (3)")
    widened = build_model("Item", "rebuild_item", {"price": IntField(), "extra": added_field})

    operations = await round_trip.migrate_to(widened)

    assert [type(operation) for operation in operations] == [AddField]
    index_names = [name for name in await round_trip.get_index_definitions("rebuild_item") if "extra" in name]
    assert len(index_names) == 1
    assert round_trip.get_pending_operations(widened) == []


@pytest.mark.asyncio
async def test_type_change_converts_stored_values_to_the_new_format(db_isolated_no_schema):
    """A rebuild copied every value as it was - a datetime text in a DATE column, a date in a
    TIMESTAMP one, 1 in a boolean one - so filters on the new type matched nothing. The values fit the
    new types - a time of day, a number other than 0 and 1 are refused before the rebuild."""
    round_trip = RoundTrip(db_isolated_no_schema.get_connection())
    await round_trip.migrate_to(
        build_model(
            "Typed",
            "rebuild_typed",
            {"moment": DatetimeField(), "day": DateField(), "count": IntField(), "number": IntField()},
        )
    )
    await round_trip.connection.execute(
        "INSERT INTO rebuild_typed (moment, day, count, number) VALUES ($1, $2, 1, 7)"
        if round_trip.dialect == "postgresql"
        else "INSERT INTO rebuild_typed (moment, day, count, number) VALUES (?, ?, 1, 7)",
        [datetime.datetime(2024, 5, 6, tzinfo=datetime.UTC), datetime.date(2024, 1, 2)],
    )
    changed = build_model(
        "Typed",
        "rebuild_typed",
        {
            "moment": DateField(),
            "day": DatetimeField(),
            "count": BooleanField(),
            # SQLite keeps no exact decimal: a FloatField there.
            "number": DecimalField(max_digits=10, decimal_places=2)
            if DatabaseUnderTest.holds_decimal_fields()
            else FloatField(),
        },
    )

    await round_trip.migrate_to(changed)

    [row] = await round_trip.connection.execute_dicts("SELECT moment, day, count, number FROM rebuild_typed")
    if round_trip.dialect == "sqlite":
        assert row["moment"] == "2024-05-06"
        assert row["day"].startswith("2024-01-02 00:00:00")
        assert row["count"] == 1
        assert row["number"] == 7.0
    else:
        assert row["moment"] == datetime.date(2024, 5, 6)
        assert row["count"] is True
        assert str(row["number"]) == "7.00"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("old_field", "stored_sql", "new_field", "message"),
    [
        (CharField(max_length=10), "'abc'", IntField(), "abc"),
        (CharField(max_length=10), "'abc'", BooleanField(), "abc"),
        (CharField(max_length=10), "'abc'", DateField(), "abc"),
        pytest.param(
            CharField(max_length=10),
            "'1.5x'",
            DecimalField(max_digits=6, decimal_places=2),
            "1.5x",
            marks=pytest.mark.decimal_fields,
        ),
    ],
)
async def test_a_type_change_refuses_a_value_the_new_field_cant_read(
    db_isolated_no_schema, old_field, stored_sql, new_field, message
):
    """SQLite copied every value into the rebuilt table: "abc" stayed in an INTEGER column, which read
    it back as no integer; PostgreSQL refuses the change itself."""
    from hare.exceptions import OperationalError

    round_trip = RoundTrip(db_isolated_no_schema.get_connection())
    await round_trip.migrate_to(build_model("Converted", "rebuild_converted", {"value": old_field}))
    await round_trip.connection.execute_script(f"INSERT INTO rebuild_converted (value) VALUES ({stored_sql})")

    with pytest.raises(OperationalError, match=message):
        await round_trip.migrate_to(build_model("Converted", "rebuild_converted", {"value": new_field}))

    assert len(await round_trip.connection.execute_dicts("SELECT value FROM rebuild_converted")) == 1


def get_number_field() -> DecimalField | FloatField:
    """A DecimalField - a FloatField on SQLite, which keeps no exact decimal."""
    if DatabaseUnderTest.holds_decimal_fields():
        return DecimalField(max_digits=6, decimal_places=2)
    return FloatField()


@pytest.mark.asyncio
async def test_a_number_made_a_boolean_converts_as_the_database_converts(db_isolated_no_schema):
    """SQLite takes a number other than zero as true; PostgreSQL refuses a numeric made a boolean."""
    from hare.exceptions import OperationalError

    round_trip = RoundTrip(db_isolated_no_schema.get_connection())
    await round_trip.migrate_to(build_model("Converted", "rebuild_converted", {"value": get_number_field()}))
    await round_trip.connection.execute_script("INSERT INTO rebuild_converted (value) VALUES (2.50), (0)")
    boolean_model = build_model("Converted", "rebuild_converted", {"value": BooleanField()})
    if round_trip.dialect != "sqlite":
        with pytest.raises(OperationalError):
            await round_trip.migrate_to(boolean_model)
        return
    await round_trip.migrate_to(boolean_model)
    rows = await round_trip.connection.execute_dicts("SELECT value FROM rebuild_converted ORDER BY value")
    assert [row["value"] for row in rows] == [0, 1]


@pytest.mark.asyncio
async def test_rebuild_of_a_table_a_view_reads_keeps_the_view_working(db_isolated_no_schema):
    """The rename of the rebuilt table re-validated every view, and failed on one naming the
    table while it was briefly missing."""
    round_trip = RoundTrip(db_isolated_no_schema.get_connection())
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
async def test_a_rebuild_does_not_use_an_existing_table_as_staging(db_isolated_no_schema):
    round_trip = RoundTrip(db_isolated_no_schema.get_connection())
    if round_trip.dialect != "sqlite":
        pytest.skip("Adding this constraint rebuilds the table on SQLite")
    await round_trip.migrate_to(build_model("Item", "rebuild_item", {"price": IntField()}))
    await round_trip.connection.execute_script(
        "CREATE TABLE new__rebuild_item (value INTEGER); INSERT INTO new__rebuild_item VALUES (99); "
        "INSERT INTO rebuild_item (id, price) VALUES (1, 7), (2, 8)"
    )
    constraint = CheckConstraint(check=RawSQLTerm("price >= 0"), name="valid_price")
    operations = await round_trip.migrate_to(
        build_model("Item", "rebuild_item", {"price": IntField()}, {"constraints": [constraint]})
    )

    assert [type(operation) for operation in operations] == [AddConstraint]
    assert await round_trip.connection.execute_dicts("SELECT price FROM rebuild_item ORDER BY id") == [
        {"price": 7},
        {"price": 8},
    ]
    assert await round_trip.connection.execute_dicts("SELECT value FROM new__rebuild_item") == [{"value": 99}]
    with pytest.raises(IntegrityError):
        await round_trip.connection.execute_script("INSERT INTO rebuild_item (id, price) VALUES (3, -1)")


@pytest.mark.asyncio
async def test_rebuild_keeps_the_autoincrement_counter(db_isolated_no_schema):
    """A rebuilt table started its AUTOINCREMENT counter from the highest id left in it, so the
    id of a deleted newest row was issued again."""
    round_trip = RoundTrip(db_isolated_no_schema.get_connection())
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


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize("schema", ["aux", "aux archive"])
async def test_attached_rebuild_keeps_its_counter_without_changing_the_main_database(
    db_isolated_no_schema, empty, schema
):
    if DatabaseUnderTest.get_dialect().name != "sqlite":
        pytest.skip("Attached databases are a SQLite schema mechanism")
    connection = db_isolated_no_schema.get_connection()
    editor = connection.dialect.schema_editor_class(connection)
    quote = editor.quote
    table = editor.qualify_table_name("rebuild_counted", schema)
    await connection.execute_script(
        f"ATTACH DATABASE ':memory:' AS {quote(schema)};"
        "CREATE TABLE main.rebuild_counted(id INTEGER PRIMARY KEY AUTOINCREMENT, quantity INT NOT NULL);"
        "INSERT INTO main.rebuild_counted(id, quantity) VALUES(1000, 4);"
        f"CREATE TABLE {table}(id INTEGER PRIMARY KEY AUTOINCREMENT, quantity INT NOT NULL);"
        f"INSERT INTO {table}(id, quantity) VALUES(1, 4), (100, 4);"
        f"DELETE FROM {table} WHERE id {'>= 1' if empty else '= 100'};"
    )
    original = build_model("Counted", "rebuild_counted", {"quantity": IntField()}, {"schema": schema})
    changed = build_model("Counted", "rebuild_counted", {"quantity": BigIntField()}, {"schema": schema})
    round_trip = RoundTrip(connection)
    round_trip.tracked_state = build_live_state(original)
    expected_rows = [] if empty else [{"id": 1, "quantity": 4}]
    for model in (changed, original):
        await round_trip.migrate_to(model)
        assert await connection.execute_dicts(f"SELECT id, quantity FROM {table}") == expected_rows
        assert await connection.execute_dicts(f"SELECT name, seq FROM {quote(schema)}.sqlite_sequence") == [
            {"name": "rebuild_counted", "seq": 100}
        ]
        assert await connection.execute_dicts("SELECT name, seq FROM main.sqlite_sequence") == [
            {"name": "rebuild_counted", "seq": 1000}
        ]
    await connection.execute_script(f"INSERT INTO {table}(quantity) VALUES(9)")
    assert await connection.execute_dicts(f"SELECT id FROM {table} WHERE quantity = 9") == [{"id": 101}]


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_partial_unique_constraint_survives_rebuilds_and_matches_drift(db_isolated_no_schema):
    """SQLite has partial indexes - a UniqueConstraint(condition=...) is created, kept by every
    rebuild of its table, enforced, and seen by drift as the declared constraint."""
    round_trip = RoundTrip(db_isolated_no_schema.get_connection())
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
    assert (await round_trip.get_drift(build_live_state(widened))).operations == []
    await round_trip.connection.execute_script(
        "INSERT INTO rebuild_partial (name, status, quantity) VALUES ('a', 'open', 0); "
        "INSERT INTO rebuild_partial (name, status, quantity) VALUES ('a', 'closed', 0)"
    )
    with pytest.raises(IntegrityError):
        await round_trip.connection.execute_script(
            "INSERT INTO rebuild_partial (name, status, quantity) VALUES ('a', 'open', 0)"
        )
