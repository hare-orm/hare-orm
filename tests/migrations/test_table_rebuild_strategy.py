"""Rebuilding a table from its model is a strategy every schema editor has, not SQLite's alone."""

import pytest

from hare.contrib.test import requires_features
from hare.ddl.constraints import UniqueConstraint
from hare.fields import CharField, IntField
from tests.migrations.test_round_trip_real_db import RoundTrip, build_model


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_rebuilt_table_keeps_its_rows_indexes_and_unique_constraints(db_isolated):
    round_trip = RoundTrip(db_isolated.db())
    widget = build_model(
        "RebuiltWidget",
        "rebuilt_widget",
        {"code": CharField(max_length=20, db_index=True), "size": IntField()},
        {"constraints": [UniqueConstraint(fields=("code", "size"), name="uq_rebuilt_widget")]},
    )
    await round_trip.migrate_to(widget)
    await round_trip.connection.execute_script(
        "INSERT INTO rebuilt_widget (id, code, size) VALUES (1, 'a', 1), (2, 'b', 2)"
    )
    indexes_before = set(await round_trip.get_index_definitions("rebuilt_widget"))

    await round_trip.editor._remake_table(widget)

    rows = await round_trip.connection.execute_dicts("SELECT id, code, size FROM rebuilt_widget ORDER BY id")
    assert rows == [{"id": 1, "code": "a", "size": 1}, {"id": 2, "code": "b", "size": 2}]
    assert set(await round_trip.get_index_definitions("rebuilt_widget")) == indexes_before
    with pytest.raises(Exception, match="(?i)unique|duplicate"):
        await round_trip.connection.execute_script("INSERT INTO rebuilt_widget (id, code, size) VALUES (3, 'a', 1)")


@pytest.mark.asyncio
async def test_rebuilt_table_keeps_its_table_and_column_comments(db_isolated):
    round_trip = RoundTrip(db_isolated.db())
    if round_trip.dialect != "postgresql":
        pytest.skip("SQLite keeps comments inside CREATE TABLE, which the rebuild writes anew")
    widget = build_model(
        "CommentedWidget",
        "commented_widget",
        {"code": CharField(max_length=20, description="The widget code")},
        {"table_description": "Widgets with comments"},
    )
    widget._no_comments = False  # type: ignore[attr-defined]
    await round_trip.migrate_to(widget)

    await round_trip.editor._remake_table(widget)

    (row,) = await round_trip.connection.execute_dicts(
        "SELECT obj_description('commented_widget'::regclass, 'pg_class') AS table_comment, "
        "col_description('commented_widget'::regclass, 2) AS column_comment"
    )
    assert row == {"table_comment": "Widgets with comments", "column_comment": "The widget code"}
