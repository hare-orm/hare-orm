"""Enums built while the application runs (``StrEnum("Status", {...})``) in migrations: the writer
declares such an enum in the migration itself, the migration state compares it by what it holds,
and a migration written from text, applied in memory and detected again after a restart with the
same enum finds nothing to do."""

from __future__ import annotations

from enum import Enum, IntEnum, IntFlag, StrEnum
from typing import Any

import pytest

from hare import Connections, Hare, fields
from hare.contrib.test import requires_features
from hare.contrib.test.databases.temporary_databases import TemporaryDatabases
from hare.ddl.constraints import CheckConstraint
from hare.declarations.declaration_value_serializer import DeclarationValueSerializer
from hare.declarations.import_manager import ImportManager
from hare.declarations.runtime_enums import RuntimeEnums
from hare.exceptions import UnSupportedError
from hare.migrations import MigrationRunner
from hare.migrations.autodetection.operation_generator import OperationGenerator
from hare.migrations.exceptions import FieldNarrowingDataLossError
from hare.migrations.migration import Migration
from hare.migrations.operations import AddField, AlterField, CreateModel
from hare.migrations.state.state import State
from hare.migrations.writer import MigrationWriter
from hare.models import Model
from hare.query.expressions import F, Q
from tests.migrations.round_trips.test_round_trip_real_db import (
    APP_LABEL,
    build_live_state,
    build_model,
    get_schema_editor,
)
from tests.utils.database_under_test import DatabaseUnderTest
from tests.utils.migration_source import MigrationSource

STATUS_MEMBERS = {"NEW": "new", "DONE": "done"}
PRIORITY_MEMBERS = {"LOW": 1, "HIGH": 2}
PERMISSION_MEMBERS = {"READ": 1, "WRITE": 2}


class ImportedStatus(StrEnum):
    NEW = "new"


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "depth,array_types,stored",
    [
        (1, "base", False),
        (1, "base", True),
        (2, "base", False),
        (2, "base", True),
        (1, "new-subclass", True),
        (1, "old-subclass", True),
        (1, "siblings", True),
        (1, "same-subclass", True),
    ],
)
@pytest.mark.parametrize("invalid", (False, True))
async def test_generated_array_output_narrowing_checks_nested_enum_members(
    db_simple, depth, array_types, stored, invalid
):
    class OriginalSize(IntEnum):
        SMALL = 1
        LARGE = 2

    class KeptSize(IntEnum):
        SMALL = 1

    class CustomArrayField(fields.ArrayField):
        pass

    class OtherArrayField(fields.ArrayField):
        pass

    old_class, new_class = {
        "base": (fields.ArrayField, fields.ArrayField),
        "new-subclass": (fields.ArrayField, CustomArrayField),
        "old-subclass": (CustomArrayField, fields.ArrayField),
        "siblings": (CustomArrayField, OtherArrayField),
        "same-subclass": (CustomArrayField, CustomArrayField),
    }[array_types]

    def array_field(enum, array_class):
        result = fields.IntEnumField(enum, null=True)
        for _ in range(depth):
            result = array_class(result)
        return result

    async with TemporaryDatabases(
        {"connections": {"main": None}, "apps": {}}, DatabaseUnderTest.get_url(), generate_schemas=False
    ) as context:
        connection = context.connections.get_main("main")
        runner = MigrationRunner(connection)
        old_output, new_output = array_field(OriginalSize, old_class), array_field(KeptSize, new_class)
        creation = Migration(
            "create_record",
            "arrays",
            operations=[
                CreateModel(
                    "Record",
                    [
                        ("id", fields.IntField(primary_key=True, generated=False)),
                        ("input", array_field(OriginalSize, old_class)),
                        (
                            "member",
                            fields.GeneratedField(
                                F("input"), old_output, stored=stored, source_field="computed member"
                            ),
                        ),
                    ],
                    options={"table": "generated_array_members"},
                )
            ],
        )
        initial = State(default_connections={"arrays": "main"})
        if not stored and await DatabaseUnderTest.get_postgresql_server_version() < (18,):
            # The runner's first checkout discovers the server version before rendering the DDL.
            with pytest.raises(UnSupportedError, match="only supports STORED generated columns"):
                await runner.apply(creation, initial)
            return
        state = await runner.apply(creation, initial)
        value = [1, 2 if invalid else 1, None]
        if depth == 2:
            value = [value]
        await state.apps.get_queryset("arrays.Record", using=connection).create(id=1, input=value)
        before = await connection.execute_dicts('SELECT id, "computed member" FROM generated_array_members')
        change = Migration(
            "narrow_member",
            "arrays",
            operations=[
                AlterField(
                    "Record",
                    "member",
                    fields.GeneratedField(F("input"), new_output, stored=stored, source_field="computed member"),
                )
            ],
        )
        if invalid:
            with pytest.raises(FieldNarrowingDataLossError):
                await runner.apply(change, state)
        else:
            result = await runner.apply(change, state)
            record = await result.apps.get_queryset("arrays.Record", using=connection).get(id=1)
            assert record.member == (
                [[KeptSize.SMALL, KeptSize.SMALL, None]] if depth == 2 else [KeptSize.SMALL, KeptSize.SMALL, None]
            )
        assert await connection.execute_dicts('SELECT id, "computed member" FROM generated_array_members') == before


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


@pytest.mark.database_independent
def test_an_enum_that_cant_be_imported_is_declared_in_the_migration():
    status, priority, permissions = build_enums()
    assert not RuntimeEnums.is_importable(status)
    assert RuntimeEnums.is_importable(ImportedStatus)
    source = write_migration(
        "0001_initial",
        OperationGenerator(State(), build_live_state(build_ticket(status, priority, permissions))).generate(),
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


@pytest.mark.database_independent
def test_an_importable_enum_is_imported_and_names_never_collide():
    imports = ImportManager()
    assert DeclarationValueSerializer.render_value(ImportedStatus.NEW, imports) == "ImportedStatus.NEW"
    assert imports.enum_declarations == []
    first_status = build_status()
    other_status = build_status({"OPEN": "open"})
    assert DeclarationValueSerializer.render_value(first_status.NEW, imports) == "Status.NEW"
    assert DeclarationValueSerializer.render_value(build_status().DONE, imports) == "Status.DONE"
    assert DeclarationValueSerializer.render_value(other_status.OPEN, imports) == "Status2.OPEN"
    assert imports.enum_declarations == [
        "Status = StrEnum('Status', {'NEW': 'new', 'DONE': 'done'})",
        "Status2 = StrEnum('Status', {'OPEN': 'open'})",
    ]


@pytest.mark.database_independent
def test_a_mixed_in_data_type_is_kept():
    colour = Enum("Colour", {"RED": "r"}, type=str)
    imports = ImportManager()
    assert DeclarationValueSerializer.render_value(colour.RED, imports) == "Colour.RED"
    assert imports.enum_declarations == ["Colour = Enum('Colour', {'RED': 'r'}, type=str)"]


@pytest.mark.database_independent
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
async def test_a_migration_written_as_text_applies_and_nothing_is_left_after_a_restart(db_isolated_no_schema):
    connection = db_isolated_no_schema.get_connection()
    editor = get_schema_editor(connection)
    tracked_state = State()
    operations = OperationGenerator(tracked_state, build_live_state(build_ticket(*build_enums()))).generate()
    assert any(isinstance(operation, CreateModel) for operation in operations)
    migration = MigrationSource.load(
        write_migration("0001_initial", operations), "0001_initial", APP_LABEL, filename="<migration 0001_initial>"
    )
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
    migration = MigrationSource.load(source, name, APP_LABEL, filename=f"<migration {name}>")
    return await migration.apply(tracked_state, dry_run=False, schema_editor=editor)


@pytest.mark.asyncio
async def test_a_live_model_with_a_runtime_enum_field(db_isolated_no_schema):
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
    connection = db_isolated_no_schema.get_connection()
    editor = get_schema_editor(connection)
    tracked_state = State()
    operations = OperationGenerator(tracked_state, build_live_state(live_ticket)).generate()
    tracked_state = await migration_apply(
        tracked_state, "0001_initial", write_migration("0001_initial", operations), editor
    )
    Hare.register_live_models([live_ticket], APP_LABEL, connection_alias=connection.connection_alias)
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


@pytest.mark.database_independent
def test_an_enum_declared_by_a_migration_file_is_declared_again():
    status = build_status()
    status.__module__ = "blog.migrations.0001_initial"
    assert not RuntimeEnums.is_importable(status)


@pytest.mark.asyncio
@requires_features(supports_transactions=True)
@pytest.mark.parametrize("field_type", ("integer", "text", "nested-array"))
async def test_removing_enum_members_checks_data_even_when_the_sql_type_is_unchanged(db_simple, field_type):
    old_enum = (
        StrEnum("Status", {"FIRST": "one", "SECOND": "two", "REMOVED": "bad"})
        if field_type == "text"
        else IntEnum("Status", {"FIRST": 1, "SECOND": 2, "REMOVED": 3})
    )
    new_enum = (
        StrEnum("Status", {"FIRST": "one", "SECOND": "two"})
        if field_type == "text"
        else IntEnum("Status", {"FIRST": 1, "SECOND": 2})
    )
    field_factory = fields.CharEnumField if field_type == "text" else fields.IntEnumField
    old_field, new_field = field_factory(old_enum), field_factory(new_enum)
    bad_value, valid_value = old_enum.REMOVED, old_enum.SECOND
    if field_type == "nested-array":
        old_field = fields.ArrayField(fields.ArrayField(old_field))
        new_field = fields.ArrayField(fields.ArrayField(new_field))
        bad_value, valid_value = [[bad_value]], [[valid_value]]
    connection = Connections.get("models")
    if not old_field.exists_on(connection.dialect):
        pytest.skip("The field has no column on this dialect")
    assert old_field.get_column_type(connection.dialect) == new_field.get_column_type(connection.dialect)
    runner = MigrationRunner(connection)
    state = await runner.apply(
        Migration(
            "create_ticket",
            APP_LABEL,
            operations=[
                CreateModel(
                    "Ticket",
                    [("id", fields.IntField(primary_key=True, generated=False)), ("status", old_field)],
                    options={"table": f"narrowed_enum_{field_type.replace('-', '_')}"},
                )
            ],
        ),
        State(default_connections={APP_LABEL: "models"}),
    )
    model_apps = state.apps
    model = model_apps.get_model(f"{APP_LABEL}.Ticket")
    await model_apps.get_queryset(model).using(connection).create(id=1, status=bad_value)
    migration = Migration("narrow_status", APP_LABEL, operations=[AlterField("Ticket", "status", new_field)])
    await runner.collect_sql(migration, state)
    assert (await model_apps.get_queryset(model).using(connection).get(id=1)).status == bad_value
    with pytest.raises(FieldNarrowingDataLossError):
        await runner.apply(migration, state)
    assert (await model_apps.get_queryset(model).using(connection).get(id=1)).status == bad_value
    await model_apps.get_queryset(model).using(connection).filter(id=1).update(status=valid_value)
    after = await runner.apply(migration, state)
    updated_model_apps = after.apps
    updated_model = updated_model_apps.get_model(f"{APP_LABEL}.Ticket")
    assert (await updated_model_apps.get_queryset(updated_model).using(connection).get(id=1)).status == valid_value
    await runner.unapply(migration, state)
    assert (await model_apps.get_queryset(model).using(connection).get(id=1)).status == valid_value
