"""Migrations of a NativeEnumField's ENUM type: the autodetector creates it before its first column,
gives it new labels and drops it after its last column; the operations written to a migration file and
read back; applied and unapplied on PostgreSQL - a label added in place, a label removed by replacing
the type with the columns and their default converted - and skipped on a database without ENUM types."""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any

import pytest

from hare import Connections, fields
from hare.contrib.test import requires_features
from hare.dialects.postgresql.fields.native_enum import NativeEnumField
from hare.exceptions import ConfigurationError, OperationalError
from hare.migrations.autodetection.operation_generator import OperationGenerator
from hare.migrations.migration import Migration
from hare.migrations.operations import (
    AddField,
    AlterEnumType,
    CreateEnumType,
    CreateModel,
    DropEnumType,
    RemoveField,
)
from hare.migrations.state.state import State
from hare.migrations.state.state_apps import StateApps
from hare.migrations.writer import MigrationWriter
from hare.transactions.transactions import Transactions

TABLE = "enum_migration_ticket"


class TicketState(Enum):
    OPEN = "open"
    CLOSED = "closed"


class TicketStateWithReview(Enum):
    OPEN = "open"
    REVIEW = "review"
    CLOSED = "closed"


class TicketStateWithoutClosed(Enum):
    OPEN = "open"
    REVIEW = "review"


def get_state(**state_fields: Any) -> State:
    state = State(models={}, apps=StateApps())
    CreateModel(
        name="Ticket",
        fields=[("id", fields.IntField(primary_key=True)), *state_fields.items()],
        options={"table": TABLE, "app": "models"},
    ).state_forward("models", state)
    return state


def test_the_type_is_created_before_its_first_column():
    operations = OperationGenerator(
        State(models={}, apps=StateApps()), get_state(state=NativeEnumField(TicketState))
    ).generate()
    assert isinstance(operations[0], CreateEnumType)
    assert (operations[0].name, operations[0].labels) == ("ticket_state", ("open", "closed"))
    assert isinstance(operations[1], CreateModel)


def test_new_labels_alter_the_type():
    old = get_state(state=NativeEnumField(TicketState))
    new = get_state(state=NativeEnumField(TicketStateWithReview, type_name="ticket_state"))
    operations = OperationGenerator(old, new).generate()
    alters = [operation for operation in operations if isinstance(operation, AlterEnumType)]
    assert [(alter.old_labels, alter.new_labels) for alter in alters] == [
        (("open", "closed"), ("open", "review", "closed"))
    ]


def test_the_type_is_dropped_after_its_last_column():
    old = get_state(state=NativeEnumField(TicketState), previous=NativeEnumField(TicketState, null=True))
    one_left = get_state(state=NativeEnumField(TicketState))
    assert not [
        operation for operation in OperationGenerator(old, one_left).generate() if isinstance(operation, DropEnumType)
    ]
    operations = OperationGenerator(one_left, get_state()).generate()
    assert [type(operation) for operation in operations] == [RemoveField, DropEnumType]


def test_two_fields_naming_one_type_with_other_labels_are_refused():
    state = get_state(
        state=NativeEnumField(TicketState), other=NativeEnumField(TicketStateWithReview, type_name="ticket_state")
    )
    with pytest.raises(ConfigurationError, match="Two fields name the ENUM type 'ticket_state'"):
        OperationGenerator(State(models={}, apps=StateApps()), state).generate()


def test_the_operations_are_written_and_read_back():
    operations = [
        CreateEnumType(name="ticket_state", labels=("open", "closed")),
        AlterEnumType(name="ticket_state", old_labels=("open", "closed"), new_labels=("open", "review", "closed")),
        DropEnumType(name="ticket_state", labels=("open", "review", "closed")),
    ]
    source = MigrationWriter("0001_initial", "models", operations).as_string()
    namespace: dict[str, Any] = {}
    exec(compile(source, "<migration>", "exec"), namespace)  # noqa: S102 - the test's own text
    read_back = namespace["Migration"]("0001_initial", "models").operations
    assert [operation.deconstruct() for operation in read_back] == [
        operation.deconstruct() for operation in operations
    ]


def make_migration(name: str, *operations) -> Migration:
    return Migration(name=name, app_label="models", operations=list(operations))


class TicketTable:
    """The ticket table and its type on the database under test, dropped afterwards."""

    def __init__(self) -> None:
        self.connection = Connections.get("models")

    def get_editor(self):
        return self.connection.dialect.schema_editor_class(self.connection, atomic=True, collect_sql=False)

    async def get_labels(self) -> list[str]:
        rows = await self.connection.execute_dicts(
            # By name at every run - a regtype literal keeps the OID of the type a cached statement saw.
            "SELECT enumlabel FROM pg_enum JOIN pg_type ON pg_type.oid = pg_enum.enumtypid "
            "WHERE pg_type.typname = 'ticket_state' ORDER BY enumsortorder"
        )
        return [row["enumlabel"] for row in rows]

    async def get_states(self) -> list[tuple[int, str | None, str | None]]:
        rows = await self.connection.execute_dicts(
            f"SELECT id, state::text AS state, history::text AS history FROM {TABLE} ORDER BY id"
        )
        return [(row["id"], row["state"], row["history"]) for row in rows]

    async def drop(self) -> None:
        await self.connection.execute_script(f"DROP TABLE IF EXISTS {TABLE}; DROP TYPE IF EXISTS ticket_state;")


@requires_features(supports_enum_types=True)
@pytest.mark.asyncio
async def test_a_label_added_and_one_removed_on_postgresql(db_simple):
    table = TicketTable()
    try:
        empty = State(models={}, apps=StateApps())
        state_field = NativeEnumField(TicketState, db_default="open")
        initial = make_migration(
            "0001_initial",
            CreateEnumType(name="ticket_state", labels=("open", "closed")),
            CreateModel(
                name="Ticket",
                fields=[("id", fields.IntField(primary_key=True)), ("state", state_field)],
                options={"table": TABLE},
            ),
            AddField("Ticket", "history", fields.TextField(null=True)),
        )
        state = await initial.apply(empty.clone(), schema_editor=table.get_editor())
        await table.connection.execute_script(
            f"INSERT INTO {TABLE} (id) VALUES (1); INSERT INTO {TABLE} (id, state) VALUES (2, 'closed')"
        )
        # An array column of the type, beside the model's - converted with it.
        await table.connection.execute_script(
            f"ALTER TABLE {TABLE} ADD COLUMN visited ticket_state[] DEFAULT '{{open}}'; "
            f"UPDATE {TABLE} SET visited = '{{open,closed}}' WHERE id = 2"
        )
        # In place: the old labels keep their order.
        added = make_migration(
            "0002_review",
            AlterEnumType(name="ticket_state", old_labels=("open", "closed"), new_labels=("open", "review", "closed")),
        )
        before_added = state.clone()
        state = await added.apply(state, schema_editor=table.get_editor())
        assert await table.get_labels() == ["open", "review", "closed"]
        await table.connection.execute_script(
            f"UPDATE {TABLE} SET state = 'review', visited = '{{open,review}}' WHERE id = 2"
        )
        # Removed: the type is replaced, the column and its default converted.
        removed = make_migration(
            "0003_without_closed",
            AlterEnumType(name="ticket_state", old_labels=("open", "review", "closed"), new_labels=("open", "review")),
        )
        state = await removed.apply(state, schema_editor=table.get_editor())
        assert await table.get_labels() == ["open", "review"]
        assert await table.get_states() == [(1, "open", None), (2, "review", None)]
        await table.connection.execute_script(f"INSERT INTO {TABLE} (id) VALUES (5)")
        visited = await table.connection.execute_dicts(
            f"SELECT id, visited::text AS visited, pg_typeof(visited)::text AS visited_type FROM {TABLE} "
            "WHERE id IN (2, 5) ORDER BY id"
        )
        assert [(row["id"], row["visited"], row["visited_type"]) for row in visited] == [
            (2, "{open,review}", "ticket_state[]"),
            (5, "{open}", "ticket_state[]"),
        ]
        await table.connection.execute_script(f"DELETE FROM {TABLE} WHERE id = 5")
        await table.connection.execute_script(f"INSERT INTO {TABLE} (id) VALUES (3)")
        assert (await table.get_states())[-1] == (3, "open", None)
        with pytest.raises(OperationalError, match="invalid input value for enum ticket_state"):
            async with Transactions.atomic():
                await table.connection.execute_script(f"INSERT INTO {TABLE} (id, state) VALUES (4, 'closed')")
        # Back: the removed label returns (a replacement again), then the added one goes.
        await removed.unapply(state, schema_editor=table.get_editor())
        assert await table.get_labels() == ["open", "review", "closed"]
        await table.connection.execute_script(f"UPDATE {TABLE} SET state = 'open', visited = '{{open}}' WHERE id = 2")
        await added.unapply(before_added, schema_editor=table.get_editor())
        assert await table.get_labels() == ["open", "closed"]
        await initial.unapply(empty, schema_editor=table.get_editor())
        rows = await table.connection.execute_dicts("SELECT 1 FROM pg_type WHERE typname = 'ticket_state'")
        assert rows == []
    finally:
        await table.drop()


@requires_features(supports_enum_types=True)
@pytest.mark.asyncio
async def test_a_text_column_of_an_enum_becomes_an_enum_column(db_simple):
    table = TicketTable()
    try:
        empty = State(models={}, apps=StateApps())
        text_state = get_state(state=fields.CharEnumField(TicketState), history=fields.TextField(null=True))
        enum_state = get_state(state=NativeEnumField(TicketState), history=fields.TextField(null=True))
        initial = make_migration("0001_initial", *OperationGenerator(empty, text_state).generate())
        state = await initial.apply(empty.clone(), schema_editor=table.get_editor())
        await table.connection.execute_script(f"INSERT INTO {TABLE} (id, state) VALUES (1, 'closed')")
        operations = OperationGenerator(text_state, enum_state).generate()
        assert [type(operation).__name__ for operation in operations] == ["CreateEnumType", "AlterField"]
        before = state.clone()
        state = await make_migration("0002_native", *operations).apply(state, schema_editor=table.get_editor())
        rows = await table.connection.execute_dicts(
            f"SELECT udt_name FROM information_schema.columns WHERE table_name = '{TABLE}' AND column_name = 'state'"
        )
        assert rows[0]["udt_name"] == "ticket_state"
        assert await table.get_states() == [(1, "closed", None)]
        await make_migration("0002_native", *operations).unapply(before, schema_editor=table.get_editor())
        rows = await table.connection.execute_dicts(
            f"SELECT data_type FROM information_schema.columns WHERE table_name = '{TABLE}' AND column_name = 'state'"
        )
        assert rows[0]["data_type"] == "character varying"
        assert await table.get_states() == [(1, "closed", None)]
    finally:
        await table.drop()


@requires_features(supports_enum_types=False)
@pytest.mark.asyncio
async def test_a_database_without_enum_types_skips_the_operations(db_simple, caplog):
    editor = Connections.get("models").dialect.schema_editor_class(
        Connections.get("models"), atomic=True, collect_sql=False
    )
    with caplog.at_level(logging.WARNING):
        await CreateEnumType(name="ticket_state", labels=("open",)).database_forward(
            "models", State(models={}, apps=StateApps()), State(models={}, apps=StateApps()), editor
        )
    assert "it has no ENUM types" in caplog.text
