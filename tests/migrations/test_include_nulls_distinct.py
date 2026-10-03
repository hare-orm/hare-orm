"""Non-key INCLUDE columns of indexes and constraints, UniqueConstraint(nulls_distinct=...) and a
deferrable ExclusionConstraint - DDL, migrations, drift and inspectdb."""

import pytest

from hare.contrib.test import requires_features
from hare.ddl import RawSQLTerm
from hare.ddl.constraints import ExclusionConstraint, UniqueConstraint
from hare.ddl.indexes import Index, PartialIndex
from hare.dialects.postgresql.constants import POSTGRESQL_DIALECT
from hare.dialects.postgresql.fields.ranges import IntRangeField
from hare.dialects.postgresql.indexes import GinIndex, GistIndex
from hare.dialects.sqlite.client import SqliteClient
from hare.dialects.sqlite.constants import SQLITE_DIALECT
from hare.exceptions import ConfigurationError, IntegrityError, UnSupportedError
from hare.fields.data.numeric import IntField
from hare.fields.data.text import CharField
from hare.inspectdb import ModelSourceGenerator, SchemaIntrospector
from hare.migrations import operations as ops
from hare.migrations.drift import detect_drift
from hare.migrations.writer import MigrationWriter
from tests.migrations.test_round_trip_real_db import APP_LABEL, RoundTrip, build_live_state, build_model


def book_fields():
    return {
        "code": CharField(max_length=20, null=True),
        "shelf": CharField(max_length=20, null=True),
        "title": CharField(max_length=50, default=""),
        "pages": IntField(default=0),
    }


def test_include_is_part_of_the_declaration():
    index = Index(fields=("title",), include=("pages",))
    assert index.deconstruct()[2] == {"fields": ["title"], "include": ["pages"]}
    assert index.get_name_parts() == ("include", "pages")
    assert index != Index(fields=("title",))
    assert hash(index) != hash(Index(fields=("title",)))
    assert "include=['pages']" in repr(index)
    assert GistIndex(fields=("title",), include=("pages",)).include == ["pages"]
    with pytest.raises(UnSupportedError, match="include= is not supported by a GIN index"):
        GinIndex(fields=("title",), include=("pages",))

    constraint = UniqueConstraint(fields=("code",), name="book_code", include=["title"], nulls_distinct=False)
    assert constraint.include == ("title",)
    assert constraint.deconstruct()[2] == {
        "fields": ["code"],
        "name": "book_code",
        "include": ["title"],
        "nulls_distinct": False,
    }
    assert constraint.get_nulls_sql(POSTGRESQL_DIALECT) == " NULLS NOT DISTINCT"
    assert (
        UniqueConstraint(fields=("code",), nulls_distinct=True).get_nulls_sql(POSTGRESQL_DIALECT) == " NULLS DISTINCT"
    )
    assert constraint.get_nulls_sql(SQLITE_DIALECT) == ""
    with pytest.raises(UnSupportedError, match="nulls_distinct is not supported by the sqlite server"):
        constraint.raise_if_unsupported(SqliteClient.features, SQLITE_DIALECT)

    exclusion = ExclusionConstraint(
        name="slot_overlap", expressions=(("during", "&&"),), include=["note"], deferrable=True
    )
    assert exclusion.deconstruct()[2]["include"] == ["note"]
    assert exclusion.deconstruct()[2]["deferrable"] is True
    with pytest.raises(ConfigurationError, match="initially_deferred requires deferrable"):
        ExclusionConstraint(name="slot_overlap", expressions=(("during", "&&"),), initially_deferred=True)


def test_migration_source_keeps_the_options():
    source = MigrationWriter(
        "0001",
        APP_LABEL,
        [
            ops.AddIndex(model_name="Book", index=Index(fields=("title",), include=("pages",))),
            ops.AddConstraint(
                model_name="Book",
                constraint=UniqueConstraint(
                    fields=("code",), name="book_code", include=("title",), nulls_distinct=False
                ),
            ),
            ops.AddConstraint(
                model_name="Slot",
                constraint=ExclusionConstraint(
                    name="slot_overlap", expressions=(("during", "&&"),), include=("note",), deferrable=True
                ),
            ),
        ],
    ).as_string()
    compile(source, "<migration>", "exec")
    assert "include=['pages']" in source
    assert "include=['title']" in source and "nulls_distinct=False" in source
    assert "include=['note']" in source and "deferrable=True" in source


@pytest.mark.asyncio
async def test_include_migrates_and_has_no_drift(db_isolated):
    round_trip = RoundTrip(db_isolated.db())
    book = build_model(
        "Book",
        "inc_book",
        book_fields(),
        {
            "indexes": [
                Index(fields=("title",), include=("pages",)),
                PartialIndex(
                    fields=("pages",), include=("title",), condition=RawSQLTerm("pages > 10"), name="inc_book_long"
                ),
                Index(fields=("shelf",), include=("code",), unique=True, name="inc_book_shelf"),
            ],
            "constraints": [UniqueConstraint(fields=("code",), name="inc_book_code", include=("title",))],
        },
    )
    await round_trip.migrate_to(book)
    assert (await detect_drift(round_trip.connection, build_live_state(book), [APP_LABEL])).operations == []
    definitions = await round_trip.get_index_definitions("inc_book")
    if round_trip.dialect == "postgresql":
        assert "INCLUDE (title)" in definitions["inc_book_long"]
        assert "INCLUDE (code)" in definitions["inc_book_shelf"]
        assert "INCLUDE (title)" in definitions["inc_book_code"]
    else:
        assert "INCLUDE" not in " ".join(definitions.values())

    changed = build_model(
        "Book",
        "inc_book",
        book_fields(),
        {
            "indexes": [
                Index(fields=("title",), include=("pages", "code")),
                PartialIndex(fields=("pages",), condition=RawSQLTerm("pages > 10"), name="inc_book_long"),
                Index(fields=("shelf",), unique=True, name="inc_book_shelf"),
            ],
            "constraints": [UniqueConstraint(fields=("code",), name="inc_book_code", include=("title", "pages"))],
        },
    )
    assert await round_trip.migrate_to(changed)
    assert round_trip.get_pending_operations(changed) == []
    assert (await detect_drift(round_trip.connection, build_live_state(changed), [APP_LABEL])).operations == []


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_nulls_distinct_and_deferrable_exclusion(db_isolated):
    round_trip = RoundTrip(db_isolated.db())
    if round_trip.dialect != "postgresql":
        book = build_model(
            "Book",
            "inc_book",
            book_fields(),
            {"constraints": [UniqueConstraint(fields=("shelf", "pages"), name="inc_book_sp", nulls_distinct=False)]},
        )
        with pytest.raises(UnSupportedError, match="nulls_distinct is not supported by the sqlite server"):
            await round_trip.migrate_to(book)
        return
    book = build_model(
        "Book",
        "inc_book",
        book_fields(),
        {
            "constraints": [
                UniqueConstraint(fields=("shelf", "pages"), name="inc_book_sp", nulls_distinct=False),
                UniqueConstraint(fields=("title", "pages"), name="inc_book_tp", nulls_distinct=True),
            ]
        },
    )
    slot = build_model(
        "Slot",
        "inc_slot",
        {"room": IntField(), "during": IntRangeField(), "note": CharField(max_length=20, default="")},
        {
            "constraints": [
                ExclusionConstraint(
                    name="inc_slot_overlap",
                    expressions=(("room", "="), ("during", "&&")),
                    include=("note",),
                    deferrable=True,
                    initially_deferred=True,
                )
            ]
        },
    )
    await round_trip.migrate_to(book, slot)
    assert (await detect_drift(round_trip.connection, build_live_state(book, slot), [APP_LABEL])).operations == []

    connection = round_trip.connection
    insert_book = 'INSERT INTO "inc_book" ("id", "pages", "title", "shelf") VALUES ($1, 1, $2, $3)'
    await connection.execute(insert_book, [1, "", None])
    with pytest.raises(IntegrityError, match="inc_book_sp"):
        await connection.execute(insert_book, [2, "y", None])
    await connection.execute(insert_book, [3, "x", "a"])

    insert_slot = 'INSERT INTO "inc_slot" ("id", "room", "during", "note") VALUES ({}, 1, \'{}\', \'\')'
    async with connection._in_transaction() as transaction:
        await transaction.execute(insert_slot.format(1, "[1,5)"))
        await transaction.execute(insert_slot.format(2, "[3,8)"))
        await transaction.execute('UPDATE "inc_slot" SET "during" = \'[5,8)\' WHERE "id" = 2')
    with pytest.raises(IntegrityError, match="inc_slot_overlap"):
        async with connection._in_transaction() as transaction:
            await transaction.execute(insert_slot.format(3, "[6,7)"))

    table_info = await SchemaIntrospector.inspect_table(round_trip.connection, "inc_slot")
    source = ModelSourceGenerator.generate_model_source(table_info, round_trip.dialect)
    assert "include=['note'], deferrable=True, initially_deferred=True" in source
    changed_slot = build_model(
        "Slot",
        "inc_slot",
        {"room": IntField(), "during": IntRangeField(), "note": CharField(max_length=20, default="")},
        {"constraints": [ExclusionConstraint(name="inc_slot_overlap", expressions=(("room", "="), ("during", "&&")))]},
    )
    assert await round_trip.migrate_to(book, changed_slot)
    assert (
        await detect_drift(round_trip.connection, build_live_state(book, changed_slot), [APP_LABEL])
    ).operations == []
