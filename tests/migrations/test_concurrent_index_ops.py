"""AddIndex / RemoveIndex with concurrently=True."""

import pytest

from hare.ddl.indexes import Index
from hare.exceptions import ConfigurationError
from hare.fields.data.numeric import IntField
from hare.fields.data.text import CharField
from hare.migrations import operations as ops
from hare.migrations.drift import detect_drift
from hare.migrations.migration import Migration
from hare.migrations.writer import MigrationWriter
from tests.migrations.test_round_trip_real_db import APP_LABEL, RoundTrip, build_live_state, build_model


def book_fields():
    return {"title": CharField(max_length=20, default=""), "rank": IntField(null=True)}


INDEX = Index(fields=("-title", "rank"), name="book_title_concurrently")


def test_migration_source():
    source = MigrationWriter(
        "0001",
        APP_LABEL,
        [
            ops.AddIndex(model_name="Book", index=INDEX, concurrently=True),
            ops.RemoveIndex(model_name="Book", name="book_title_concurrently", concurrently=True),
        ],
    ).as_string()
    compile(source, "<migration>", "exec")
    assert source.count("concurrently=True") == 2
    assert (
        ops.AddIndex(model_name="Book", index=INDEX, concurrently=True).describe()
        == "Concurrently add index book_title_concurrently to Book"
    )


async def apply_non_atomic(round_trip: RoundTrip, operations: list, *, collect_sql: bool = False) -> Migration:
    editor = type(round_trip.editor)(round_trip.connection, atomic=False, collect_sql=collect_sql)
    migration = Migration(name="0099_concurrently", app_label=APP_LABEL)
    migration.atomic = False
    migration.operations = operations
    round_trip.tracked_state = await migration.apply(round_trip.tracked_state, dry_run=False, schema_editor=editor)
    return migration


@pytest.mark.asyncio
async def test_concurrent_index_operations_build_and_drop_the_index(db_isolated):
    round_trip = RoundTrip(db_isolated.db())
    book = build_model("Book", "conc_book", book_fields())
    await round_trip.migrate_to(book)
    before = round_trip.tracked_state.clone()

    migration = await apply_non_atomic(round_trip, [ops.AddIndex(model_name="Book", index=INDEX, concurrently=True)])
    indexed = build_model("Book", "conc_book", book_fields(), {"indexes": [INDEX]})
    assert round_trip.get_pending_operations(indexed) == []
    assert (await detect_drift(round_trip.connection, build_live_state(indexed), [APP_LABEL])).operations == []
    assert "book_title_concurrently" in await round_trip.get_index_definitions("conc_book")

    editor = type(round_trip.editor)(round_trip.connection, atomic=False, collect_sql=False)
    round_trip.tracked_state = await migration.unapply(before, dry_run=False, schema_editor=editor)
    assert "book_title_concurrently" not in await round_trip.get_index_definitions("conc_book")

    await apply_non_atomic(round_trip, [ops.AddIndex(model_name="Book", index=INDEX, concurrently=True)])
    await apply_non_atomic(
        round_trip, [ops.RemoveIndex(model_name="Book", name="book_title_concurrently", concurrently=True)]
    )
    assert "book_title_concurrently" not in await round_trip.get_index_definitions("conc_book")
    assert (await detect_drift(round_trip.connection, build_live_state(book), [APP_LABEL])).operations == []


@pytest.mark.asyncio
async def test_concurrent_index_needs_a_non_atomic_migration_on_postgres(db_isolated):
    round_trip = RoundTrip(db_isolated.db())
    if round_trip.dialect != "postgresql":
        pytest.skip("SQLite builds the index the plain way inside the migration's transaction")
    await round_trip.migrate_to(build_model("Book", "conc_book", book_fields()))
    with pytest.raises(ConfigurationError, match="set atomic = False on the migration"):
        await round_trip.apply([ops.AddIndex(model_name="Book", index=INDEX, concurrently=True)])

    editor = type(round_trip.editor)(round_trip.connection, atomic=True, collect_sql=True)
    migration = Migration(name="0099_concurrently", app_label=APP_LABEL)
    migration.operations = [ops.AddIndex(model_name="Book", index=INDEX, concurrently=True)]
    await migration.apply(round_trip.tracked_state.clone(), dry_run=False, schema_editor=editor)
    assert any("CREATE INDEX CONCURRENTLY" in statement for statement in editor.collected_sql)
