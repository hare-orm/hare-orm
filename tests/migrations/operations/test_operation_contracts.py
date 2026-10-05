"""What each operation tells about itself - its description, the models it names, what it squashes into,
what it does to the data - and the defaults of an operation declaring none of it."""

from types import SimpleNamespace

import pytest

from hare.dialects.dialect_registry import DialectRegistry
from hare.fields import CharField, IntField
from hare.migrations.operations import (
    AddPartition,
    AlterColumnNotNullSafe,
    AlterEnumType,
    AlterModelOptions,
    AlterModelSchema,
    AlterModelTable,
    AlterSequence,
    AlterTrigger,
    BackfillColumn,
    CreateCollation,
    CreateEnumType,
    CreateModel,
    DeleteModel,
    DropEnumType,
    DropSchema,
    RefreshMaterializedView,
    RemoveCollation,
    RemoveIndex,
    RemoveTrigger,
    RenameIndex,
    RenameModel,
    RenameTrigger,
    SynchronizeKeySeries,
    ValidateConstraint,
)
from hare.migrations.operations.dependencies.operation_dependency import OperationDependency
from hare.migrations.operations.enums import DependencyType
from hare.migrations.operations.hare_operation import HareOperation
from hare.migrations.state.state import State
from tests.utils.fake_client import FakeClient

APP_LABEL = "shop"
SQLITE = DialectRegistry.get_dialect("sqlite")


class PlainOperation(HareOperation):
    """An operation declaring nothing of its own."""


def get_item_state() -> State:
    state = State()
    CreateModel(
        name="Item",
        fields=[("id", IntField(primary_key=True)), ("code", CharField(max_length=5, null=True))],
    ).state_forward(APP_LABEL, state)
    return state


@pytest.mark.database_independent
@pytest.mark.parametrize(
    ("operation", "description"),
    [
        (BackfillColumn("Item", "code", "x"), "Backfill code on Item"),
        (ValidateConstraint("Item", "item_code_check"), "Validate constraint item_code_check on Item"),
        (AlterEnumType("mood", ["a"], ["a", "b"], schema="space"), "Alter ENUM type space.mood"),
        (CreateEnumType("mood", ["a"]), "Create ENUM type mood"),
        (DropEnumType("mood", ["a"], schema="space"), "Drop ENUM type space.mood"),
        (CreateCollation("german", "de-DE"), "Create collation german"),
        (RemoveCollation("german", "de-DE"), "Remove collation german"),
        (AlterModelOptions("Item", {}), "Alter options for Item"),
        (AlterModelSchema("Item", "archive"), "Move table for Item to schema 'archive'"),
        (AddPartition("Item", SimpleNamespace(name="item_2026")), "Add partition item_2026 to Item"),
        (SynchronizeKeySeries("Item"), "Synchronize the key series of Item"),
        (PlainOperation(), "PlainOperation"),
    ],
    ids=lambda value: type(value).__name__ if not isinstance(value, str) else None,
)
def test_an_operation_describes_itself(operation, description):
    assert operation.describe() == description


@pytest.mark.database_independent
@pytest.mark.parametrize(
    ("operation", "labels"),
    [
        (CreateEnumType("mood", ["a"]), frozenset()),
        (DropEnumType("mood", ["a"]), frozenset()),
        (AlterModelOptions("Item", {}), frozenset({"shop.item"})),
        (AlterModelSchema("Item", "archive"), frozenset({"shop.item"})),
        (AlterModelTable("Item", "goods"), frozenset({"shop.item"})),
        (RenameModel("Item", "Good"), frozenset({"shop.item", "shop.good"})),
        (AddPartition("Item", SimpleNamespace(name="item_2026")), frozenset({"shop.item"})),
    ],
    ids=lambda value: type(value).__name__ if not isinstance(value, frozenset) else None,
)
def test_an_operation_names_the_models_it_touches(operation, labels):
    assert operation.get_referenced_model_labels(APP_LABEL) == labels


@pytest.mark.database_independent
@pytest.mark.parametrize("operation_class", [AlterModelOptions, AlterModelTable], ids=lambda value: value.__name__)
def test_a_later_change_of_the_model_takes_the_place_of_an_options_or_table_change(operation_class):
    earlier = operation_class("Item", {}) if operation_class is AlterModelOptions else operation_class("Item", "a")
    later = operation_class("item", {"x": 1}) if operation_class is AlterModelOptions else operation_class("item", "b")
    deleted = DeleteModel("Item")
    assert earlier.reduce(later, APP_LABEL) == [later]
    assert earlier.reduce(deleted, APP_LABEL) == [deleted]
    # Another model's operation moves across it.
    assert earlier.reduce(DeleteModel("Other"), APP_LABEL) is True


@pytest.mark.database_independent
@pytest.mark.parametrize(
    ("operation", "reaches_other_models"),
    [
        (RefreshMaterializedView("Item", "item_totals"), True),
        (AlterTrigger("Item", SimpleNamespace(name="item_audit")), True),
        (RemoveTrigger("Item", "item_audit"), True),
        (RenameTrigger("Item", "item_audit", "item_audit_new"), True),
        (AlterSequence("Item", SimpleNamespace(name="item_number")), False),
        (RemoveIndex("Item", "item_code"), False),
        (RenameIndex("Item", "item_code_new", old_name="item_code"), False),
    ],
    ids=lambda value: type(value).__name__ if not isinstance(value, bool) else None,
)
def test_an_operation_running_sql_against_any_table_may_touch_any_model(operation, reaches_other_models):
    assert operation.reaches_other_models() is reaches_other_models


@pytest.mark.database_independent
def test_the_effect_of_an_operation():
    state = get_item_state()
    assert PlainOperation().get_effect(APP_LABEL, state, SQLITE).loses_data is False
    dropped = DeleteModel("Item").get_effect(APP_LABEL, state, SQLITE)
    assert (dropped.loses_data, dropped.reason) == (True, "drops the table of Item with its rows")
    state_only = DeleteModel("Item", state_only=True).get_effect(APP_LABEL, state, SQLITE)
    assert state_only.loses_data is False


@pytest.mark.database_independent
def test_an_unmet_requirement_names_what_the_other_app_has_to_do():
    requirement = OperationDependency.of_model("billing", "Invoice", DependencyType.CREATED)
    message = PlainOperation().get_unmet_requirement_message(APP_LABEL, requirement)
    assert message == (
        f"PlainOperation (app 'shop') needs {requirement.subject} billing.invoice {requirement.dependency_type} "
        "first - run makemigrations for 'billing' in the same run."
    )


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_an_operation_without_database_work_runs_none():
    state = get_item_state()
    editor = SQLITE.schema_editor_class(FakeClient("sqlite"))
    for operation in (PlainOperation(), SynchronizeKeySeries("Item")):
        operation.state_forward(APP_LABEL, state)
        await operation.database_forward(APP_LABEL, state, state, editor)
        await operation.database_backward(APP_LABEL, state, state, editor)
    assert editor.client.executed == []


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_a_dropped_schema_and_a_removed_collation_are_created_again_going_back():
    state = State()
    client = FakeClient("postgresql")
    editor = DialectRegistry.get_dialect("postgresql").schema_editor_class(client)
    await DropSchema("archive").database_backward(APP_LABEL, state, state, editor)
    await RemoveCollation("german", "de-DE").database_backward(APP_LABEL, state, state, editor)
    assert client.executed[0] == 'CREATE SCHEMA IF NOT EXISTS "archive";'
    assert client.executed[1].startswith('CREATE COLLATION "german"')


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_a_safe_not_null_alteration_going_back_makes_the_column_nullable_again():
    before = get_item_state()
    after = before.clone()
    operation = AlterColumnNotNullSafe("Item", "code")
    operation.state_forward(APP_LABEL, after)
    altered: list[tuple[bool, bool, str]] = []

    async def alter_field(old_model, new_model, field_name):
        altered.append(
            (old_model._meta.fields_map[field_name].null, new_model._meta.fields_map[field_name].null, field_name)
        )

    # Going back, the state after the operation is left for the one before it.
    await operation.database_backward(APP_LABEL, after, before, SimpleNamespace(alter_field=alter_field))
    await operation.database_backward(APP_LABEL, after, before, None)
    assert altered == [(False, True, "code")]
