"""Index key order: descending fields, F().asc()/.desc() keys with NULL placement, collated keys -
DDL, migrations, drift and inspectdb; and RenameField/RemoveField of an index's keys and include."""

import pytest

from hare.contrib.test import requires_features
from hare.ddl.constraints import UniqueConstraint
from hare.ddl.indexes import Index, OrderedIndexKey, PartialIndex
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.exceptions import ConfigurationError, UnSupportedError
from hare.fields.data.numeric import IntField
from hare.fields.data.text import CharField
from hare.inspectdb import ModelSourceGenerator, SchemaIntrospector
from hare.migrations import operations as ops
from hare.migrations.drift import detect_drift
from hare.migrations.writer import MigrationWriter
from hare.query.expressions import F
from hare.sql.enums import Order
from tests.migrations.test_round_trip_real_db import APP_LABEL, RoundTrip, build_live_state, build_model


def book_fields():
    return {
        "title": CharField(max_length=20, default=""),
        "rank": IntField(null=True),
        "score": IntField(default=0),
    }


def test_descending_fields_and_ordered_keys():
    index = Index(fields=("-title", "rank"))
    assert index.fields == ["title", "rank"]
    assert index.field_orders == ["DESC", ""]
    assert index.deconstruct()[2] == {"fields": ["-title", "rank"]}
    assert index.get_name_parts() == ("desc", "title")
    assert index != Index(fields=("title", "rank"))
    assert Index(F("title").desc(), F("rank")) == index
    assert Index(F("rank").desc(nulls_last=True)).expressions != ()
    assert Index(F("rank").desc(nulls_first=True)).get_key_orders() == Index(fields=("-rank",)).get_key_orders()
    assert Index(F("rank").asc(nulls_last=True)).get_key_orders() == [Order.ASC_NULLS_LAST]


def test_migration_source_keeps_key_orders():
    source = MigrationWriter(
        "0001",
        APP_LABEL,
        [
            ops.AddIndex(model_name="Book", index=Index(fields=("-title", "rank"))),
            ops.AddIndex(model_name="Book", index=Index(F("rank").desc(nulls_last=True), name="book_rank")),
            ops.AddIndex(
                model_name="Book",
                index=Index(OrderedIndexKey(RawSQLTerm('"rank"'), Order.ASC_NULLS_FIRST), name="book_rank_first"),
            ),
        ],
    ).as_string()
    compile(source, "<migration>", "exec")
    assert "fields=['-title', 'rank']" in source
    assert "F('rank').desc(nulls_last=True)" in source
    assert "OrderedIndexKey(RawSQLTerm('\"rank\"'), Order.ASC_NULLS_FIRST)" in source


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_key_orders_migrate_have_no_drift_and_come_back_from_inspectdb(db_isolated):
    round_trip = RoundTrip(db_isolated.db())
    is_postgres = round_trip.dialect == "postgresql"
    indexes = [
        Index(fields=("-title", "rank")),
        Index(fields=("-score",), unique=True),
        Index(F("rank").desc(), F("score"), name="keyo_f_desc"),
        PartialIndex(fields=("-rank",), condition=RawSQLTerm("rank > 1"), name="keyo_partial_desc"),
    ]
    if is_postgres:
        indexes += [
            Index(F("rank").desc(nulls_last=True), name="keyo_nulls_last"),
            Index(F("rank").asc(nulls_first=True), F("score"), name="keyo_mixed"),
            Index(F("score").desc(nulls_first=True), name="keyo_explicit_default"),
        ]
    book = build_model("Book", "keyo", book_fields(), {"indexes": indexes})
    await round_trip.migrate_to(book)
    assert round_trip.get_pending_operations(book) == []
    assert (await detect_drift(round_trip.connection, build_live_state(book), [APP_LABEL])).operations == []
    definitions = " ".join((await round_trip.get_index_definitions("keyo")).values())
    assert "DESC" in definitions

    table_info = await SchemaIntrospector.inspect_table(round_trip.connection, "keyo")
    source = ModelSourceGenerator.generate_model_source(table_info, round_trip.dialect)
    compile(source, "<generated>", "exec")
    assert "Index(fields=['-title', 'rank'])" in source
    assert "Index(fields=['-score'], unique=True)" in source
    assert "Index(fields=['-rank', 'score'], name='keyo_f_desc')" in source
    assert "PartialIndex(fields=['-rank'], name='keyo_partial_desc', condition=RawSQLTerm('rank > 1'))" in source
    assert "TODO" not in source
    if is_postgres:
        assert "Index(F('rank').desc(nulls_last=True), name='keyo_nulls_last')" in source
        assert "Index(F('rank').asc(nulls_first=True), F('score'), name='keyo_mixed')" in source

    changed = build_model(
        "Book",
        "keyo",
        book_fields(),
        {"indexes": [Index(fields=("title", "-rank")), Index(F("rank"), F("score").desc(), name="keyo_f_desc")]},
    )
    assert await round_trip.migrate_to(changed)
    assert (await detect_drift(round_trip.connection, build_live_state(changed), [APP_LABEL])).operations == []


@pytest.mark.asyncio
async def test_null_placement_of_an_index_key_is_rejected_on_sqlite(db_isolated):
    round_trip = RoundTrip(db_isolated.db())
    if round_trip.dialect != "sqlite":
        pytest.skip("SQLite indexes can't place NULLs")
    book = build_model(
        "Book", "keyo", book_fields(), {"indexes": [Index(F("rank").desc(nulls_last=True), name="keyo_nulls")]}
    )
    with pytest.raises(UnSupportedError, match="NULL placement"):
        await round_trip.migrate_to(book)


@pytest.mark.asyncio
async def test_rename_field_keeps_key_order_and_include(db_isolated):
    round_trip = RoundTrip(db_isolated.db())
    book = build_model(
        "Book",
        "keyo",
        book_fields(),
        {
            "indexes": [
                Index(fields=("-title", "rank"), include=("score",), name="keyo_title"),
                PartialIndex(
                    fields=("-rank",), include=("score",), condition=RawSQLTerm("rank > 1"), name="keyo_rank"
                ),
            ],
            "constraints": [UniqueConstraint(fields=("title",), include=("score",), name="keyo_unique_title")],
        },
    )
    await round_trip.migrate_to(book)
    await round_trip.apply([ops.RenameField(model_name="Book", old_name="score", new_name="points")])
    await round_trip.apply([ops.RenameField(model_name="Book", old_name="title", new_name="heading")])
    renamed_fields = {"heading": book_fields()["title"], "rank": IntField(null=True), "points": IntField(default=0)}
    renamed = build_model(
        "Book",
        "keyo",
        renamed_fields,
        {
            "indexes": [
                Index(fields=("-heading", "rank"), include=("points",), name="keyo_title"),
                PartialIndex(
                    fields=("-rank",), include=("points",), condition=RawSQLTerm("rank > 1"), name="keyo_rank"
                ),
            ],
            "constraints": [UniqueConstraint(fields=("heading",), include=("points",), name="keyo_unique_title")],
        },
    )
    assert round_trip.get_pending_operations(renamed) == []
    assert (await detect_drift(round_trip.connection, build_live_state(renamed), [APP_LABEL])).operations == []
    with pytest.raises(ConfigurationError, match="include columns of Index 'keyo_title'"):
        await round_trip.apply([ops.RemoveField(model_name="Book", name="points")])
