"""Real-database regression tests for renames touching relations, raw SQL and triggers:
RenameModel/AlterModelTable carrying automatic M2M through tables along, FK/O2O attribute
renames, RenameField leaving raw SQL alone when the column stays put, trigger recreation after a
column rename, CreateModel with both a CheckConstraint and a trigger, and BackfillColumn binding
non-primitive values."""

from __future__ import annotations

import datetime
import json
import uuid
from typing import Any

import pytest

from hare.contrib.test import requires_features
from hare.ddl import RawSQLTerm
from hare.ddl.constraints import CheckConstraint, UniqueConstraint
from hare.ddl.enums import TriggerTiming
from hare.ddl.indexes import Index
from hare.ddl.triggers import Trigger
from hare.dialects.postgresql.indexes import HnswIndex
from hare.fields import ForeignKeyField, ManyToManyField
from hare.fields.data.boolean import BooleanField
from hare.fields.data.json import JSONField
from hare.fields.data.numeric import IntField
from hare.fields.data.temporal import TimeDeltaField
from hare.fields.data.text import CharField
from hare.fields.data.uuids import UUIDField
from hare.migrations.autodetection.operation_generator import OperationGenerator
from hare.migrations.migration import Migration
from hare.migrations.operations import (
    AddField,
    AddIndex,
    AlterField,
    BackfillColumn,
    RemoveField,
    RemoveIndex,
    RenameField,
)
from hare.migrations.operations.base import HareOperation
from hare.migrations.state.project import State, StateApps
from hare.models import Model
from tests.migrations.test_round_trip_real_db import APP_LABEL, RoundTrip, build_live_state, build_model


def build_auto_table_model(model_name: str, fields: dict[str, Any]) -> type[Model]:
    """A live model whose table name is derived from the class name (no Meta.table)."""
    attributes: dict[str, Any] = {"id": IntField(primary_key=True), **fields}
    attributes["Meta"] = type("Meta", (), {"app": APP_LABEL})
    attributes["_no_comments"] = True
    return type(model_name, (Model,), attributes)


class ReversibleRoundTrip(RoundTrip):
    """RoundTrip that can also roll the last applied migration back."""

    def __init__(self, connection) -> None:
        super().__init__(connection)
        self.history: list[tuple[Migration, State]] = []

    async def apply(self, operations: list[HareOperation]) -> None:
        self.migration_count += 1
        migration = Migration(name=f"{self.migration_count:04d}_step", app_label=APP_LABEL)
        migration.operations = list(operations)
        state_before = self.tracked_state.clone()
        async with self.editor.constraint_checking_disabled():
            self.tracked_state = await migration.apply(self.tracked_state, dry_run=False, schema_editor=self.editor)
        self.history.append((migration, state_before))

    async def roll_back(self) -> None:
        migration, state_before = self.history.pop()
        async with self.editor.constraint_checking_disabled():
            await migration.unapply(state_before.clone(), schema_editor=self.editor)
        self.tracked_state = state_before

    async def query(self, sql: str) -> list[dict[str, Any]]:
        return await self.connection.execute_dicts(sql)

    async def get_column_names(self, table: str) -> list[str]:
        if self.dialect == "sqlite":
            rows = await self.query(f"PRAGMA table_info('{table}')")
            return sorted(row["name"] for row in rows)
        rows = await self.query(
            "SELECT column_name FROM information_schema.columns "
            f"WHERE table_name = '{table}' AND table_schema = current_schema()"
        )
        return sorted(row["column_name"] for row in rows)


@pytest.fixture
def reversible_round_trip(db_isolated) -> ReversibleRoundTrip:
    return ReversibleRoundTrip(db_isolated.db())


@pytest.mark.asyncio
async def test_rename_model_renames_auto_m2m_through_table_and_key_column(
    reversible_round_trip: ReversibleRoundTrip,
) -> None:
    round_trip = reversible_round_trip
    author = build_auto_table_model("RtAuthor", {"name": CharField(max_length=10)})
    book = build_auto_table_model("RtBook", {"tags": ManyToManyField("models.RtAuthor", related_name="tagged_books")})
    await round_trip.migrate_to(author, book)
    await round_trip.query("INSERT INTO rtauthor (id, name) VALUES (1, 'a')")
    await round_trip.query("INSERT INTO rtbook (id) VALUES (1)")
    await round_trip.query("INSERT INTO rtbook_rtauthor (rtbook_id, rtauthor_id) VALUES (1, 1)")

    writer = build_auto_table_model("RtWriter", {"name": CharField(max_length=10)})
    renamed_book = build_auto_table_model(
        "RtBook", {"tags": ManyToManyField("models.RtWriter", related_name="tagged_books")}
    )
    await round_trip.migrate_to(writer, renamed_book)

    assert await round_trip.query("SELECT rtbook_id, rtwriter_id FROM rtbook_rtwriter") == [
        {"rtbook_id": 1, "rtwriter_id": 1}
    ]
    assert round_trip.get_pending_operations(writer, renamed_book) == []

    await round_trip.roll_back()

    assert await round_trip.query("SELECT rtbook_id, rtauthor_id FROM rtbook_rtauthor") == [
        {"rtbook_id": 1, "rtauthor_id": 1}
    ]
    assert round_trip.get_pending_operations(author, book) == []


@pytest.mark.asyncio
async def test_rename_model_with_explicit_table_renames_m2m_key_column(
    reversible_round_trip: ReversibleRoundTrip,
) -> None:
    round_trip = reversible_round_trip
    author = build_model("Author", "rt_m2m_author", {"name": CharField(max_length=10)})
    book = build_model("Book", "rt_m2m_book", {"tags": ManyToManyField("models.Author", related_name="tagged_books")})
    await round_trip.migrate_to(author, book)
    await round_trip.query("INSERT INTO rt_m2m_author (id, name) VALUES (1, 'a')")
    await round_trip.query("INSERT INTO rt_m2m_book (id) VALUES (1)")
    await round_trip.query("INSERT INTO rt_m2m_book_rt_m2m_author (rt_m2m_book_id, author_id) VALUES (1, 1)")

    writer = build_model("Writer", "rt_m2m_author", {"name": CharField(max_length=10)})
    renamed_book = build_model(
        "Book", "rt_m2m_book", {"tags": ManyToManyField("models.Writer", related_name="tagged_books")}
    )
    await round_trip.migrate_to(writer, renamed_book)

    assert await round_trip.get_column_names("rt_m2m_book_rt_m2m_author") == ["rt_m2m_book_id", "writer_id"]
    assert await round_trip.query("SELECT writer_id FROM rt_m2m_book_rt_m2m_author") == [{"writer_id": 1}]
    assert round_trip.get_pending_operations(writer, renamed_book) == []

    await round_trip.roll_back()

    assert await round_trip.get_column_names("rt_m2m_book_rt_m2m_author") == ["author_id", "rt_m2m_book_id"]
    assert round_trip.get_pending_operations(author, book) == []


@pytest.mark.asyncio
async def test_alter_model_table_renames_auto_m2m_through_table(
    reversible_round_trip: ReversibleRoundTrip,
) -> None:
    round_trip = reversible_round_trip
    author = build_model("Author", "rt_tbl_author", {"name": CharField(max_length=10)})
    book = build_model("Book", "rt_tbl_book", {"tags": ManyToManyField("models.Author", related_name="tagged_books")})
    await round_trip.migrate_to(author, book)
    await round_trip.query("INSERT INTO rt_tbl_author (id, name) VALUES (1, 'a')")
    await round_trip.query("INSERT INTO rt_tbl_book (id) VALUES (1)")
    await round_trip.query("INSERT INTO rt_tbl_book_rt_tbl_author (rt_tbl_book_id, author_id) VALUES (1, 1)")

    moved_author = build_model("Author", "rt_tbl_writers", {"name": CharField(max_length=10)})
    moved_book = build_model(
        "Book", "rt_tbl_books", {"tags": ManyToManyField("models.Author", related_name="tagged_books")}
    )
    await round_trip.migrate_to(moved_author, moved_book)

    assert await round_trip.query("SELECT rt_tbl_books_id, author_id FROM rt_tbl_books_rt_tbl_writers") == [
        {"rt_tbl_books_id": 1, "author_id": 1}
    ]
    assert round_trip.get_pending_operations(moved_author, moved_book) == []

    await round_trip.roll_back()

    assert await round_trip.query("SELECT rt_tbl_book_id, author_id FROM rt_tbl_book_rt_tbl_author") == [
        {"rt_tbl_book_id": 1, "author_id": 1}
    ]
    assert round_trip.get_pending_operations(author, book) == []


@pytest.mark.asyncio
async def test_renaming_a_foreign_key_attribute_generates_rename_field(
    reversible_round_trip: ReversibleRoundTrip,
) -> None:
    round_trip = reversible_round_trip
    author = build_model("Author", "rt_fk_author", {"name": CharField(max_length=10)})
    book = build_model("Book", "rt_fk_book", {"author": ForeignKeyField("models.Author", related_name="books")})
    await round_trip.migrate_to(author, book)
    await round_trip.query("INSERT INTO rt_fk_author (id, name) VALUES (1, 'a')")
    await round_trip.query("INSERT INTO rt_fk_book (id, author_id) VALUES (1, 1)")

    renamed_book = build_model(
        "Book", "rt_fk_book", {"writer": ForeignKeyField("models.Author", related_name="books")}
    )
    operations = await round_trip.migrate_to(author, renamed_book)

    assert [type(operation) for operation in operations] == [RemoveIndex, RenameField, AddIndex]
    assert await round_trip.query("SELECT id, writer_id FROM rt_fk_book") == [{"id": 1, "writer_id": 1}]
    assert round_trip.get_pending_operations(author, renamed_book) == []

    await round_trip.roll_back()

    assert await round_trip.query("SELECT id, author_id FROM rt_fk_book") == [{"id": 1, "author_id": 1}]


@pytest.mark.asyncio
async def test_renaming_a_foreign_key_onto_its_existing_column_keeps_the_column(
    reversible_round_trip: ReversibleRoundTrip,
) -> None:
    round_trip = reversible_round_trip
    author = build_model("Author", "rt_fkcol_author", {"name": CharField(max_length=10)})
    book = build_model("Book", "rt_fkcol_book", {"author": ForeignKeyField("models.Author", related_name="books")})
    await round_trip.migrate_to(author, book)
    await round_trip.query("INSERT INTO rt_fkcol_author (id, name) VALUES (1, 'a')")
    await round_trip.query("INSERT INTO rt_fkcol_book (id, author_id) VALUES (1, 1)")

    renamed_book = build_model(
        "Book",
        "rt_fkcol_book",
        {"writer": ForeignKeyField("models.Author", related_name="written_books", source_field="author_id")},
    )
    operations = await round_trip.migrate_to(author, renamed_book)

    assert [type(operation) for operation in operations] == [RemoveIndex, RenameField, AddIndex]
    assert await round_trip.get_column_names("rt_fkcol_book") == ["author_id", "id"]
    assert await round_trip.query("SELECT id, author_id FROM rt_fkcol_book") == [{"id": 1, "author_id": 1}]
    assert round_trip.get_pending_operations(author, renamed_book) == []


@pytest.mark.asyncio
async def test_renaming_and_altering_a_foreign_key_creates_its_index_once(
    reversible_round_trip: ReversibleRoundTrip,
) -> None:
    """A rename that also alters the relation rebuilds the table on SQLite - the rebuild used to
    re-create the relation's index from the field, and the AddIndex after it failed on the index
    the rebuild had already made."""
    round_trip = reversible_round_trip
    author = build_model("Author", "rt_fkalter_author", {"name": CharField(max_length=10)})
    book = build_model("Book", "rt_fkalter_book", {"author": ForeignKeyField("models.Author", related_name="books")})
    await round_trip.migrate_to(author, book)

    renamed_book = build_model(
        "Book",
        "rt_fkalter_book",
        {"writer": ForeignKeyField("models.Author", related_name="books", source_field="author_id", null=True)},
    )
    operations = await round_trip.migrate_to(author, renamed_book)

    assert [type(operation) for operation in operations] == [RemoveIndex, RenameField, AlterField, AddIndex]
    assert await round_trip.get_column_names("rt_fkalter_book") == ["author_id", "id"]
    assert round_trip.get_pending_operations(author, renamed_book) == []


@pytest.mark.asyncio
async def test_unrecognized_foreign_key_rename_removes_the_old_relation_first(
    reversible_round_trip: ReversibleRoundTrip,
) -> None:
    round_trip = reversible_round_trip
    author = build_model("Author", "rt_fkswap_author", {"name": CharField(max_length=10)})
    book = build_model("Book", "rt_fkswap_book", {"author": ForeignKeyField("models.Author", related_name="books")})
    await round_trip.migrate_to(author, book)

    replaced_book = build_model(
        "Book", "rt_fkswap_book", {"editor": ForeignKeyField("models.Author", related_name="books", null=True)}
    )
    operations = await round_trip.migrate_to(author, replaced_book)

    assert [type(operation) for operation in operations] == [RemoveIndex, RemoveField, AddField, AddIndex]
    assert await round_trip.get_column_names("rt_fkswap_book") == ["editor_id", "id"]
    assert round_trip.get_pending_operations(author, replaced_book) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("column", ["writer_id", "custom_author_column"])
async def test_alter_field_on_foreign_key_with_source_field_keeps_its_column(
    reversible_round_trip: ReversibleRoundTrip, column: str
) -> None:
    round_trip = reversible_round_trip
    author = build_model("Author", "rt_fksrc_author", {"name": CharField(max_length=10)})
    book = build_model(
        "Book",
        "rt_fksrc_book",
        {"writer": ForeignKeyField("models.Author", related_name="books", source_field=column)},
    )
    await round_trip.migrate_to(author, book)
    await round_trip.query("INSERT INTO rt_fksrc_author (id, name) VALUES (1, 'a')")
    await round_trip.query(f"INSERT INTO rt_fksrc_book (id, {column}) VALUES (1, 1)")

    altered_book = build_model(
        "Book",
        "rt_fksrc_book",
        {"writer": ForeignKeyField("models.Author", related_name="books", source_field=column, null=True)},
    )
    await round_trip.migrate_to(author, altered_book)

    assert await round_trip.get_column_names("rt_fksrc_book") == sorted(["id", column])
    assert await round_trip.query(f"SELECT id, {column} FROM rt_fksrc_book") == [{"id": 1, column: 1}]

    await round_trip.roll_back()

    assert await round_trip.query(f"SELECT id, {column} FROM rt_fksrc_book") == [{"id": 1, column: 1}]


@pytest.mark.asyncio
async def test_rename_field_keeping_its_column_leaves_raw_sql_untouched(
    reversible_round_trip: ReversibleRoundTrip,
) -> None:
    round_trip = reversible_round_trip
    constraints = [CheckConstraint(check=RawSQLTerm("x > 0"), name="rt_rawsql_x_positive")]
    old_model = build_model("Record", "rt_rawsql", {"x": IntField(), "z": IntField()}, {"constraints": constraints})
    await round_trip.migrate_to(old_model)

    renamed_model = build_model(
        "Record", "rt_rawsql", {"y": IntField(source_field="x"), "z": IntField()}, {"constraints": constraints}
    )
    await round_trip.migrate_to(renamed_model)

    assert round_trip.tracked_state.models[(APP_LABEL, "Record")].options["constraints"] == tuple(constraints)
    assert round_trip.get_pending_operations(renamed_model) == []

    altered_model = build_model(
        "Record",
        "rt_rawsql",
        {"y": IntField(source_field="x"), "z": IntField(null=True)},
        {"constraints": constraints},
    )
    await round_trip.migrate_to(altered_model)
    assert round_trip.get_pending_operations(altered_model) == []


@pytest.mark.asyncio
async def test_rename_field_recreates_a_trigger_referencing_the_column(
    reversible_round_trip: ReversibleRoundTrip,
) -> None:
    round_trip = reversible_round_trip
    if round_trip.dialect == "postgresql":
        old_body = "BEGIN NEW.y := NEW.x * 2; RETURN NEW; END;"
        new_body = "BEGIN NEW.y := NEW.w * 2; RETURN NEW; END;"
        timing = TriggerTiming.BEFORE
    else:
        old_body = "UPDATE rt_trigger_rename SET y = NEW.x * 2 WHERE id = NEW.id;"
        new_body = "UPDATE rt_trigger_rename SET y = NEW.w * 2 WHERE id = NEW.id;"
        timing = TriggerTiming.AFTER
    old_model = build_model(
        "Record",
        "rt_trigger_rename",
        {"x": IntField(), "y": IntField(null=True)},
        {"triggers": [Trigger(name="rt_trigger_double", on="INSERT", timing=timing, body=old_body)]},
    )
    await round_trip.migrate_to(old_model)

    await round_trip.apply([RenameField("Record", "x", "w")])
    new_model = build_model(
        "Record",
        "rt_trigger_rename",
        {"w": IntField(), "y": IntField(null=True)},
        {"triggers": [Trigger(name="rt_trigger_double", on="INSERT", timing=timing, body=new_body)]},
    )
    assert round_trip.get_pending_operations(new_model) == []

    await round_trip.query("INSERT INTO rt_trigger_rename (id, w) VALUES (1, 7)")
    assert await round_trip.query("SELECT w, y FROM rt_trigger_rename") == [{"w": 7, "y": 14}]

    await round_trip.roll_back()

    await round_trip.query("INSERT INTO rt_trigger_rename (id, x) VALUES (2, 5)")
    assert await round_trip.query("SELECT x, y FROM rt_trigger_rename WHERE id = 2") == [{"x": 5, "y": 10}]


@pytest.mark.asyncio
async def test_renaming_a_trigger_while_changing_its_deferral_applies_the_deferral(
    reversible_round_trip: ReversibleRoundTrip,
) -> None:
    round_trip = reversible_round_trip
    if round_trip.dialect != "postgresql":
        pytest.skip("Deferrable constraint triggers are Postgres-only")
    body = "BEGIN RETURN NEW; END;"
    old_model = build_model(
        "Record",
        "rt_trigger_deferral",
        {"x": IntField()},
        {"triggers": [Trigger(name="rt_trigger_old", on="INSERT", body=body)]},
    )
    await round_trip.migrate_to(old_model)

    new_model = build_model(
        "Record",
        "rt_trigger_deferral",
        {"x": IntField()},
        {
            "triggers": [
                Trigger(name="rt_trigger_new", on="INSERT", body=body, deferrable=True, initially_deferred=True)
            ]
        },
    )
    await round_trip.migrate_to(new_model)

    assert await round_trip.query(
        "SELECT tgname, tgdeferrable, tginitdeferred FROM pg_trigger WHERE tgname LIKE 'rt_trigger_%'"
    ) == [{"tgname": "rt_trigger_new", "tgdeferrable": True, "tginitdeferred": True}]
    assert round_trip.get_pending_operations(new_model) == []


@pytest.mark.asyncio
async def test_create_model_with_check_constraint_and_trigger(reversible_round_trip: ReversibleRoundTrip) -> None:
    round_trip = reversible_round_trip
    body = "BEGIN RETURN NEW; END;" if round_trip.dialect == "postgresql" else "SELECT 1;"
    model = build_model(
        "Record",
        "rt_create_check_trigger",
        {"x": IntField()},
        {
            "triggers": [Trigger(name="rt_create_trigger", on="INSERT", timing=TriggerTiming.BEFORE, body=body)],
            "constraints": [CheckConstraint(check=RawSQLTerm("x >= 0"), name="rt_create_check")],
        },
    )
    await round_trip.migrate_to(model)

    assert round_trip.get_pending_operations(model) == []
    await round_trip.query("INSERT INTO rt_create_check_trigger (id, x) VALUES (1, 1)")
    with pytest.raises(Exception, match="(?i)check|constraint"):
        await round_trip.query("INSERT INTO rt_create_check_trigger (id, x) VALUES (2, -1)")


@pytest.mark.asyncio
async def test_backfill_column_converts_values_with_the_field(reversible_round_trip: ReversibleRoundTrip) -> None:
    round_trip = reversible_round_trip
    model = build_model(
        "Record",
        "rt_backfill_values",
        {
            "payload": JSONField(null=True),
            "token": UUIDField(null=True),
            "duration": TimeDeltaField(null=True),
        },
    )
    await round_trip.migrate_to(model)
    await round_trip.query("INSERT INTO rt_backfill_values (id) VALUES (1)")

    token = uuid.UUID(int=5)
    await round_trip.apply(
        [
            BackfillColumn("Record", "payload", {"a": 1}),
            BackfillColumn("Record", "token", token),
            BackfillColumn("Record", "duration", datetime.timedelta(seconds=90)),
        ]
    )

    rows = await round_trip.query("SELECT payload, token, duration FROM rt_backfill_values")
    assert len(rows) == 1
    payload = rows[0]["payload"]
    assert (json.loads(payload) if isinstance(payload, str) else payload) == {"a": 1}
    token_field = model._meta.fields_map["token"]
    assert round_trip.connection.dialect.types.get_python_value(token_field, rows[0]["token"]) == token
    assert rows[0]["duration"] == 90_000_000


def test_rename_field_keeps_vector_index_parameters_in_its_condition() -> None:
    old_model = build_model(
        "Record",
        "rt_hnsw",
        {"x": IntField(), "flag": BooleanField()},
        {
            "indexes": [
                HnswIndex(
                    fields=("x",),
                    name="rt_hnsw_index",
                    m=8,
                    opclasses=("vector_l2_ops",),
                    condition=RawSQLTerm("flag"),
                )
            ]
        },
    )
    state = State(models={}, apps=StateApps())
    for operation in OperationGenerator(state, build_live_state(old_model)).generate():
        operation.state_forward(APP_LABEL, state)

    RenameField("Record", "flag", "active").state_forward(APP_LABEL, state)

    renamed_index = state.models[(APP_LABEL, "Record")].options["indexes"][0]
    assert renamed_index.extra == " WITH (m=8, ef_construction=64) WHERE (active)"
    new_model = build_model(
        "Record",
        "rt_hnsw",
        {"x": IntField(), "active": BooleanField()},
        {
            "indexes": [
                HnswIndex(
                    fields=("x",),
                    name="rt_hnsw_index",
                    m=8,
                    opclasses=("vector_l2_ops",),
                    condition=RawSQLTerm("active"),
                )
            ]
        },
    )
    assert OperationGenerator(state, build_live_state(new_model)).generate() == []


def build_generated_names_model(model_name: str, with_indexes: bool = True) -> type[Model]:
    """A model whose indexes and unique constraints all get names generated from its table."""
    meta_options: dict[str, Any] = {"app": APP_LABEL}
    if with_indexes:
        meta_options["indexes"] = [Index(fields=("a", "c")), Index(fields=("b",), unique=True)]
        meta_options["constraints"] = [UniqueConstraint(fields=("a", "b")), UniqueConstraint(fields=("b", "c"))]
    attributes: dict[str, Any] = {
        "id": IntField(primary_key=True),
        "a": IntField(),
        "b": IntField(),
        "c": IntField(),
        "d": IntField(db_index=with_indexes),
        "Meta": type("Meta", (), meta_options),
        "_no_comments": True,
    }
    return type(model_name, (Model,), attributes)


async def get_index_and_constraint_names(round_trip: ReversibleRoundTrip, table: str) -> list[str]:
    names = {name for name in await round_trip.get_index_definitions(table) if not name.endswith("_pkey")}
    if round_trip.dialect != "sqlite":
        rows = await round_trip.query(
            "SELECT conname FROM pg_constraint WHERE conrelid = "
            f"(quote_ident(current_schema()) || '.' || quote_ident('{table}'))::regclass AND contype = 'u'"
        )
        names.update(row["conname"] for row in rows)
    return sorted(names)


@pytest.mark.asyncio
async def test_rename_model_renames_the_generated_index_and_constraint_names(
    reversible_round_trip: ReversibleRoundTrip,
) -> None:
    """RenameModel left the idx_/uidx_/uid_ names generated from the old table in place - the next
    migration dropping one of them computed the new table's name and failed with "no such index"."""
    round_trip = reversible_round_trip
    await round_trip.migrate_to(build_generated_names_model("RtGenOld"))
    old_names = await get_index_and_constraint_names(round_trip, "rtgenold")

    renamed_model = build_generated_names_model("RtGenNew")
    await round_trip.migrate_to(renamed_model)

    new_names = await get_index_and_constraint_names(round_trip, "rtgennew")
    assert len(new_names) == len(old_names)
    assert not [name for name in new_names if "rtgenold" in name]
    assert round_trip.get_pending_operations(renamed_model) == []

    stripped_model = build_generated_names_model("RtGenNew", with_indexes=False)
    await round_trip.migrate_to(stripped_model)

    assert await get_index_and_constraint_names(round_trip, "rtgennew") == []
    assert round_trip.get_pending_operations(stripped_model) == []

    await round_trip.roll_back()
    await round_trip.roll_back()

    # SQLite re-adds the dropped unique_together as a standalone index rather than inline.
    rolled_back_names = await get_index_and_constraint_names(round_trip, "rtgenold")
    assert set(old_names) <= set(rolled_back_names)
    assert not [name for name in rolled_back_names if "rtgennew" in name]


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_rename_constraint_of_an_inlined_unique_constraint_leaves_one_constraint(
    reversible_round_trip: ReversibleRoundTrip,
) -> None:
    """On SQLite, renaming a UniqueConstraint inlined into CREATE TABLE rebuilt the table with the
    new one inline and then added it a second time as a standalone index - and rolling back left
    the new name inline next to the old one's standalone index."""
    round_trip = reversible_round_trip
    fields_by_name = {"a": IntField(), "b": IntField()}
    check_constraint = CheckConstraint(check=RawSQLTerm("a >= 0"), name="ck_rt_inline_a")
    old_model = build_model(
        "Inline",
        "rt_inline_unique",
        fields_by_name,
        {"constraints": [check_constraint, UniqueConstraint(fields=("a", "b"), name="uq_rt_inline_old")]},
    )
    new_model = build_model(
        "Inline",
        "rt_inline_unique",
        fields_by_name,
        {"constraints": [check_constraint, UniqueConstraint(fields=("a", "b"), name="uq_rt_inline_new")]},
    )
    await round_trip.migrate_to(old_model)

    await round_trip.migrate_to(new_model)

    if round_trip.dialect == "sqlite":
        assert list(await round_trip.get_index_definitions("rt_inline_unique")) == ["uq_rt_inline_new"]
    else:
        assert await get_index_and_constraint_names(round_trip, "rt_inline_unique") == ["uq_rt_inline_new"]
    assert round_trip.get_pending_operations(new_model) == []

    await round_trip.roll_back()

    if round_trip.dialect == "sqlite":
        assert list(await round_trip.get_index_definitions("rt_inline_unique")) == ["uq_rt_inline_old"]
    else:
        assert await get_index_and_constraint_names(round_trip, "rt_inline_unique") == ["uq_rt_inline_old"]
