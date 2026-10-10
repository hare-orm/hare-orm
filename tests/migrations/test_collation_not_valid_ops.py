"""CreateCollation / RemoveCollation and AddConstraint(not_valid=True) / ValidateConstraint."""

import pytest

from hare.ddl.constraints import CheckConstraint, UniqueConstraint
from hare.exceptions import ConfigurationError, IntegrityError
from hare.fields.data.numeric import IntField
from hare.fields.data.text import CharField
from hare.inspectdb import ModelSourceGenerator
from hare.inspectdb.introspection.database_catalog import DatabaseCatalog
from hare.migrations import operations as ops
from hare.migrations.drift import detect_drift
from hare.migrations.migration import Migration
from hare.migrations.writer import MigrationWriter
from hare.query.expressions import Q
from tests.migrations.test_round_trip_real_db import APP_LABEL, RoundTrip, build_live_state, build_model

CHECK = CheckConstraint(check=Q(rank__gte=0), name="nv_rank_positive")


def book_fields():
    return {"title": CharField(max_length=20, default=""), "rank": IntField(default=0)}


def test_migration_source_and_arguments():
    source = MigrationWriter(
        "0001",
        APP_LABEL,
        [
            ops.CreateCollation("case_insensitive", "und-u-ks-level2", provider="icu", deterministic=False),
            ops.RemoveCollation("case_insensitive", "und-u-ks-level2", provider="icu", deterministic=False),
            ops.AddConstraint(model_name="Book", constraint=CHECK, not_valid=True),
            ops.ValidateConstraint(model_name="Book", name="nv_rank_positive"),
        ],
    ).as_string()
    compile(source, "<migration>", "exec")
    assert (
        "ops.CreateCollation(name='case_insensitive', locale='und-u-ks-level2', provider='icu', deterministic=False)"
        in source
    )
    assert "ops.RemoveCollation(" in source
    assert "not_valid=True" in source
    assert "ops.ValidateConstraint(model_name='Book', name='nv_rank_positive')" in source
    with pytest.raises(ConfigurationError, match="takes a CheckConstraint"):
        ops.AddConstraint(model_name="Book", constraint=UniqueConstraint(fields=("title",), name="u"), not_valid=True)
    with pytest.raises(ConfigurationError, match="Invalid collation name"):
        ops.CreateCollation("bad name", "en")
    with pytest.raises(ConfigurationError, match="provider"):
        ops.CreateCollation("fine", "en", provider="other")


@pytest.mark.asyncio
async def test_not_valid_check_constraint_then_validate(db_isolated):
    round_trip = RoundTrip(db_isolated.get_connection())
    book = build_model("Book", "nv_book", book_fields())
    await round_trip.migrate_to(book)
    connection = round_trip.connection
    is_postgres = round_trip.dialect == "postgresql"
    if is_postgres:
        await connection.execute('INSERT INTO "nv_book" ("id", "title", "rank") VALUES (1, \'old\', -5)')

    await round_trip.apply([ops.AddConstraint(model_name="Book", constraint=CHECK, not_valid=True)])
    checked = build_model("Book", "nv_book", book_fields(), {"constraints": [CHECK]})
    assert round_trip.get_pending_operations(checked) == []
    assert (await detect_drift(connection, build_live_state(checked), [APP_LABEL])).operations == []
    with pytest.raises(IntegrityError):
        await connection.execute('INSERT INTO "nv_book" ("id", "title", "rank") VALUES (2, \'new\', -1)')
    if not is_postgres:
        await round_trip.apply([ops.ValidateConstraint(model_name="Book", name="nv_rank_positive")])
        return

    source = ModelSourceGenerator.generate_model_source(
        await DatabaseCatalog.inspect_table(connection, "nv_book"), round_trip.dialect
    )
    assert "CheckConstraint(check=RawSQLTerm('rank >= 0'), name='nv_rank_positive')" in source
    assert "# TODO: CHECK constraint 'nv_rank_positive' is not validated in the database" in source
    with pytest.raises(IntegrityError):
        await round_trip.apply([ops.ValidateConstraint(model_name="Book", name="nv_rank_positive")])
    await connection.execute('UPDATE "nv_book" SET "rank" = 0 WHERE "id" = 1')
    await round_trip.apply([ops.ValidateConstraint(model_name="Book", name="nv_rank_positive")])
    source = ModelSourceGenerator.generate_model_source(
        await DatabaseCatalog.inspect_table(connection, "nv_book"), round_trip.dialect
    )
    assert "NOT VALID" not in source


@pytest.mark.asyncio
async def test_create_and_remove_collation(db_isolated):
    round_trip = RoundTrip(db_isolated.get_connection())
    await round_trip.migrate_to(build_model("Book", "nv_book", book_fields()))
    create = ops.CreateCollation("nv_case_insensitive", "und-u-ks-level2", provider="icu", deterministic=False)
    before = round_trip.tracked_state.clone()
    migration = Migration(name="0099_collation", app_label=APP_LABEL)
    migration.operations = [create]
    round_trip.tracked_state = await migration.apply(
        round_trip.tracked_state, dry_run=False, schema_editor=round_trip.editor
    )
    connection = round_trip.connection
    if round_trip.dialect != "postgresql":
        await migration.unapply(before, dry_run=False, schema_editor=round_trip.editor)
        return
    await connection.execute('INSERT INTO "nv_book" ("id", "title", "rank") VALUES (1, \'Hello\', 0)')
    rows = await connection.execute_dicts(
        'SELECT "id" FROM "nv_book" WHERE "title" = \'HELLO\' COLLATE "nv_case_insensitive"'
    )
    assert [row["id"] for row in rows] == [1]
    await migration.unapply(before, dry_run=False, schema_editor=round_trip.editor)
    collations = await connection.execute_dicts(
        "SELECT collname FROM pg_collation WHERE collname = 'nv_case_insensitive'"
    )
    assert collations == []

    remove = Migration(name="0100_collation", app_label=APP_LABEL)
    remove.operations = [
        ops.CreateCollation("nv_case_insensitive", "und-u-ks-level2", provider="icu", deterministic=False),
        ops.RemoveCollation("nv_case_insensitive", "und-u-ks-level2", provider="icu", deterministic=False),
    ]
    await remove.apply(before.clone(), dry_run=False, schema_editor=round_trip.editor)
    collations = await connection.execute_dicts(
        "SELECT collname FROM pg_collation WHERE collname = 'nv_case_insensitive'"
    )
    assert collations == []
