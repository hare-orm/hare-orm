"""Enums built while the application runs (``StrEnum("Status", {...})``) in migrations: the writer
declares such an enum in the migration itself, the migration state compares it by what it holds,
and a migration written from text, applied in memory and detected again after a restart with the
same enum finds nothing to do."""

from __future__ import annotations

from enum import Enum, IntEnum, IntFlag, StrEnum
from typing import Any

import pytest

from hare import Hare, fields
from hare.ddl.constraints import CheckConstraint
from hare.migrations.autodetection.operation_generator import OperationGenerator
from hare.migrations.migration import Migration
from hare.migrations.operations import AddField, AlterField, CreateModel
from hare.migrations.runtime_enums import RuntimeEnums
from hare.migrations.state.project import State, StateApps
from hare.migrations.writer import ImportManager, MigrationWriter
from hare.models import Model
from hare.query.expressions import Q
from tests.migrations.test_round_trip_real_db import APP_LABEL, build_live_state, build_model, get_schema_editor

STATUS_MEMBERS = {"NEW": "new", "DONE": "done"}
PRIORITY_MEMBERS = {"LOW": 1, "HIGH": 2}
PERMISSION_MEMBERS = {"READ": 1, "WRITE": 2}


class ImportedStatus(StrEnum):
    NEW = "new"


def build_status(members: dict[str, str] | None = None) -> type[StrEnum]:
    return StrEnum("Status", STATUS_MEMBERS if members is None else members)


def build_ticket(status: type[StrEnum], priority: type[IntEnum], permissions: type[IntFlag]) -> type[Model]:
    return build_model(
        "Ticket",
        "enum_ticket",
        {
            "status": fields.CharEnumField(status, default=status.NEW),
            "previous_status": fields.CharEnumField(status, null=True),
            "priority": fields.IntEnumField(priority, default=priority.LOW),
            "permissions": fields.IntEnumField(permissions, default=permissions.READ | permissions.WRITE),
        },
        {
            "constraints": [
                CheckConstraint(check=~Q(status=status.NEW, priority=priority.HIGH), name="enum_ticket_done")
            ]
        },
    )


def build_enums() -> tuple[type[StrEnum], type[IntEnum], type[IntFlag]]:
    return (
        build_status(),
        IntEnum("Priority", PRIORITY_MEMBERS),  # type: ignore[return-value]
        IntFlag("Permission", PERMISSION_MEMBERS),  # type: ignore[return-value]
    )


def write_migration(name: str, operations: list[Any]) -> str:
    return MigrationWriter(name, APP_LABEL, operations).as_string()


def load_migration(name: str, source: str) -> Migration:
    """Builds a migration from its text in memory, as a panel keeping migrations in its database does."""
    namespace: dict[str, Any] = {}
    exec(compile(source, f"<migration {name}>", "exec"), namespace)  # noqa: S102 - the test's own text
    return namespace["Migration"](name, APP_LABEL)


def test_an_enum_that_cant_be_imported_is_declared_in_the_migration():
    status, priority, permissions = build_enums()
    assert not RuntimeEnums.is_importable(status)
    assert RuntimeEnums.is_importable(ImportedStatus)
    source = write_migration(
        "0001_initial",
        OperationGenerator(
            State(models={}, apps=StateApps()), build_live_state(build_ticket(status, priority, permissions))
        ).generate(),
    )
    assert "Status = StrEnum('Status', {'NEW': 'new', 'DONE': 'done'})" in source
    assert "Priority = IntEnum('Priority', {'LOW': 1, 'HIGH': 2})" in source
    assert "Permission = IntFlag('Permission', {'READ': 1, 'WRITE': 2})" in source
    assert source.count("Status = StrEnum(") == 1
    assert "default=Status.NEW" in source
    assert "enum_type=Status" in source
    assert "default=Permission(3)" in source
    assert "Status.NEW" in source and "Priority.HIGH" in source
    assert "__main__" not in source and "import Status" not in source
    assert "from enum import IntEnum, IntFlag, StrEnum" in source


def test_an_importable_enum_is_imported_and_names_never_collide():
    imports = ImportManager()
    assert MigrationWriter.render_value(ImportedStatus.NEW, imports) == "ImportedStatus.NEW"
    assert imports.enum_declarations == []
    first_status = build_status()
    other_status = build_status({"OPEN": "open"})
    assert MigrationWriter.render_value(first_status.NEW, imports) == "Status.NEW"
    assert MigrationWriter.render_value(build_status().DONE, imports) == "Status.DONE"
    assert MigrationWriter.render_value(other_status.OPEN, imports) == "Status2.OPEN"
    assert imports.enum_declarations == [
        "Status = StrEnum('Status', {'NEW': 'new', 'DONE': 'done'})",
        "Status2 = StrEnum('Status', {'OPEN': 'open'})",
    ]


def test_a_mixed_in_data_type_is_kept():
    colour = Enum("Colour", {"RED": "r"}, type=str)
    imports = ImportManager()
    assert MigrationWriter.render_value(colour.RED, imports) == "Colour.RED"
    assert imports.enum_declarations == ["Colour = Enum('Colour', {'RED': 'r'}, type=str)"]


def test_the_same_enum_built_again_compares_equal_and_a_change_is_detected():
    tracked_state = build_live_state(build_ticket(*build_enums()))
    assert OperationGenerator(tracked_state, build_live_state(build_ticket(*build_enums()))).generate() == []
    changed_members = [
        {"NEW": "new", "DONE": "done", "HELD": "held"},
        {"NEW": "new"},
        {"NEW": "new", "DONE": "finished"},
        {"DONE": "done", "NEW": "new"},
    ]
    for members in changed_members:
        _, priority, permissions = build_enums()
        operations = OperationGenerator(
            tracked_state, build_live_state(build_ticket(build_status(members), priority, permissions))
        ).generate()
        altered = [operation for operation in operations if isinstance(operation, AlterField)]
        assert {operation.name for operation in altered} == {"status", "previous_status"}, members


@pytest.mark.asyncio
async def test_a_migration_written_as_text_applies_and_nothing_is_left_after_a_restart(db_isolated):
    connection = db_isolated.db()
    editor = get_schema_editor(connection)
    tracked_state = State(models={}, apps=StateApps())
    operations = OperationGenerator(tracked_state, build_live_state(build_ticket(*build_enums()))).generate()
    assert any(isinstance(operation, CreateModel) for operation in operations)
    migration = load_migration("0001_initial", write_migration("0001_initial", operations))
    tracked_state = await migration.apply(tracked_state, dry_run=False, schema_editor=editor)
    assert OperationGenerator(tracked_state, build_live_state(build_ticket(*build_enums()))).generate() == []

    status = build_status({"NEW": "new", "DONE": "done", "HELD": "held"})
    _, priority, permissions = build_enums()
    ticket = build_ticket(status, priority, permissions)
    operations = OperationGenerator(tracked_state, build_live_state(ticket)).generate()
    source = write_migration("0002_status", operations)
    assert "Status = StrEnum('Status', {'NEW': 'new', 'DONE': 'done', 'HELD': 'held'})" in source
    tracked_state = await migration_apply(tracked_state, "0002_status", source, editor)
    assert OperationGenerator(tracked_state, build_live_state(ticket)).generate() == []

    await connection.execute_script(
        "INSERT INTO enum_ticket (id, status, previous_status, priority, permissions) VALUES (1, 'held', NULL, 2, 3)"
    )
    rows = await connection.execute_dicts("SELECT status, priority, permissions FROM enum_ticket")
    assert [(row["status"], row["priority"], row["permissions"]) for row in rows] == [("held", 2, 3)]


async def migration_apply(tracked_state: State, name: str, source: str, editor: Any) -> State:
    return await load_migration(name, source).apply(tracked_state, dry_run=False, schema_editor=editor)


@pytest.mark.asyncio
async def test_a_live_model_with_a_runtime_enum_field(db_isolated):
    status = build_status()
    live_ticket = type(
        "LiveEnumTicket",
        (Model,),
        {
            "__module__": __name__,
            "id": fields.IntField(primary_key=True),
            "status": fields.CharEnumField(status, default=status.NEW),
            "Meta": type("Meta", (), {"table": "live_enum_ticket"}),
        },
    )
    connection = db_isolated.db()
    editor = get_schema_editor(connection)
    tracked_state = State(models={}, apps=StateApps())
    operations = OperationGenerator(tracked_state, build_live_state(live_ticket)).generate()
    tracked_state = await migration_apply(
        tracked_state, "0001_initial", write_migration("0001_initial", operations), editor
    )
    Hare.register_live_models([live_ticket], APP_LABEL, connection_alias=connection.connection_name)
    try:
        await live_ticket.objects.create(id=1)
        assert (await live_ticket.objects.get(id=1)).status is status.NEW
        rebuilt_status = build_status()
        rebuilt_ticket = type(
            "LiveEnumTicket",
            (Model,),
            {
                "__module__": __name__,
                "id": fields.IntField(primary_key=True),
                "status": fields.CharEnumField(rebuilt_status, default=rebuilt_status.NEW),
                "Meta": type("Meta", (), {"table": "live_enum_ticket"}),
            },
        )
        assert OperationGenerator(tracked_state, build_live_state(rebuilt_ticket)).generate() == []
        added = OperationGenerator(
            tracked_state,
            build_live_state(
                type(
                    "LiveEnumTicket",
                    (Model,),
                    {
                        "__module__": __name__,
                        "id": fields.IntField(primary_key=True),
                        "status": fields.CharEnumField(rebuilt_status, default=rebuilt_status.NEW),
                        "next_status": fields.CharEnumField(rebuilt_status, null=True),
                        "Meta": type("Meta", (), {"table": "live_enum_ticket"}),
                    },
                )
            ),
        ).generate()
        assert [type(operation) for operation in added] == [AddField]
    finally:
        Hare.unregister_live_models([live_ticket])


def test_an_enum_declared_by_a_migration_file_is_declared_again():
    status = build_status()
    status.__module__ = "blog.migrations.0001_initial"
    assert not RuntimeEnums.is_importable(status)
