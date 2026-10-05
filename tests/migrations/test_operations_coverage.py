from __future__ import annotations

import pytest

from hare import fields
from hare.ddl import RawSQLTerm
from hare.ddl.constraints import CheckConstraint, UniqueConstraint
from hare.ddl.indexes import Index
from hare.ddl.schema_objects.trigger import Trigger
from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
from hare.dialects.base.schema.tables.table_comments import TableComments
from hare.exceptions import ConfigurationError
from hare.migrations.exceptions import IncompatibleStateError, IrreversibleMigrationError
from hare.migrations.operations import (
    AddConstraint,
    AddField,
    AddIndex,
    AddTrigger,
    AlterField,
    AlterTrigger,
    RemoveConstraint,
    RemoveField,
    RemoveIndex,
    RemoveTrigger,
    RenameConstraint,
    RenameField,
    RenameIndex,
    RenameModel,
    RenameTrigger,
    RunPython,
    RunSQL,
    SQLOperation,
)
from hare.migrations.state.state import State
from hare.migrations.state.state_apps import StateApps
from tests.utils.fake_client import FakeClient


class TestSchemaEditorTableComments(TableComments):
    def get_table_comment_sql(self, table: str, comment: str) -> str:
        return ""

    def get_column_comment_sql(self, table: str, column: str, comment: str) -> str:
        return ""


class _TestSchemaEditor(BaseSchemaEditor):
    table_comments_class = TestSchemaEditorTableComments


# --- IncompatibleStateError guards, one per operation type ---


def test_rename_model_missing_source_raises(empty_state: State):
    with pytest.raises(IncompatibleStateError):
        RenameModel(old_name="Ghost", new_name="Renamed").state_forward("models", empty_state)


def test_add_field_already_present_raises(state_with_model: State):
    with pytest.raises(IncompatibleStateError):
        AddField(model_name="TestModel", name="id", field=fields.IntField(primary_key=True)).state_forward(
            "models", state_with_model
        )


def test_remove_field_missing_raises(state_with_model: State):
    with pytest.raises(IncompatibleStateError):
        RemoveField(model_name="TestModel", name="ghost").state_forward("models", state_with_model)


def test_alter_field_missing_raises(state_with_model: State):
    with pytest.raises(IncompatibleStateError):
        AlterField(model_name="TestModel", name="ghost", field=fields.TextField()).state_forward(
            "models", state_with_model
        )


def test_rename_field_missing_source_raises(state_with_model: State):
    with pytest.raises(IncompatibleStateError):
        RenameField(model_name="TestModel", old_name="ghost", new_name="renamed").state_forward(
            "models", state_with_model
        )


def test_rename_field_target_already_present_raises(state_with_model: State):
    AddField(model_name="TestModel", name="name", field=fields.TextField()).state_forward("models", state_with_model)
    with pytest.raises(IncompatibleStateError):
        RenameField(model_name="TestModel", old_name="id", new_name="name").state_forward("models", state_with_model)


def test_remove_index_missing_raises(state_with_model: State):
    with pytest.raises(IncompatibleStateError):
        RemoveIndex(model_name="TestModel", name="ghost_idx").state_forward("models", state_with_model)


def test_rename_index_missing_old_name_raises(state_with_model: State):
    with pytest.raises(IncompatibleStateError):
        RenameIndex(model_name="TestModel", new_name="renamed_idx", old_name="ghost_idx").state_forward(
            "models", state_with_model
        )


def test_remove_constraint_missing_raises(state_with_model: State):
    with pytest.raises(IncompatibleStateError):
        RemoveConstraint(model_name="TestModel", name="ghost").state_forward("models", state_with_model)


def test_rename_constraint_missing_old_name_raises(state_with_model: State):
    with pytest.raises(IncompatibleStateError):
        RenameConstraint(model_name="TestModel", old_name="ghost", new_name="renamed").state_forward(
            "models", state_with_model
        )


def test_remove_trigger_missing_raises(state_with_model: State):
    with pytest.raises(IncompatibleStateError):
        RemoveTrigger(model_name="TestModel", name="ghost_trig").state_forward("models", state_with_model)


def test_alter_trigger_missing_raises(state_with_model: State):
    trigger = Trigger(name="ghost_trig", on="INSERT", body=RawSQLTerm("SELECT 1;"))
    with pytest.raises(IncompatibleStateError):
        AlterTrigger(model_name="TestModel", trigger=trigger).state_forward("models", state_with_model)


def test_rename_trigger_missing_old_name_raises(state_with_model: State):
    with pytest.raises(IncompatibleStateError):
        RenameTrigger(model_name="TestModel", old_name="ghost_trig", new_name="renamed_trig").state_forward(
            "models", state_with_model
        )


# --- Constructor ValueError guards - "name or fields"/"old_name or old_fields" style mutual
# requirements checked eagerly at __init__ time, before state_forward ever runs ---


def test_remove_index_requires_name_or_fields():
    with pytest.raises(ConfigurationError, match="name or fields"):
        RemoveIndex(model_name="TestModel")


def test_rename_index_requires_old_name_or_old_fields():
    with pytest.raises(ConfigurationError, match="old_name or old_fields"):
        RenameIndex(model_name="TestModel", new_name="renamed_idx")


def test_rename_index_old_name_and_old_fields_mutually_exclusive():
    with pytest.raises(ConfigurationError, match="mutually exclusive"):
        RenameIndex(model_name="TestModel", new_name="renamed_idx", old_name="idx_id", old_fields=["id"])


def test_remove_constraint_requires_name_or_fields():
    with pytest.raises(ConfigurationError, match="name or fields"):
        RemoveConstraint(model_name="TestModel")


# --- Alternate lookup paths: fields=/old_fields= - each of these is a real, separately-reachable
# code path from the name-based one already exercised above ---


def test_remove_index_by_fields_matches_index_instance(state_with_model: State):
    index = Index(fields=("id",), name="idx_id")
    AddIndex(model_name="TestModel", index=index).state_forward("models", state_with_model)
    RemoveIndex(model_name="TestModel", fields=["id"]).state_forward("models", state_with_model)
    model_state = state_with_model.models[("models", "TestModel")]
    assert model_state.get_option_list("indexes") == []


def test_remove_index_by_fields_matches_raw_field_list(state_with_model: State):
    """An index recorded in state as a plain field-name list/tuple (not an Index instance) -
    e.g. from a Meta.indexes shorthand entry - must resolve via the same fields= lookup."""
    model_state = state_with_model.models[("models", "TestModel")]
    model_state.set_option_list("indexes", [("id",)])
    RemoveIndex(model_name="TestModel", fields=["id"]).state_forward("models", state_with_model)
    assert model_state.get_option_list("indexes") == []


def test_rename_index_by_old_fields_success(state_with_model: State):
    RenameIndex(model_name="TestModel", new_name="idx_renamed", old_fields=["id"]).state_forward(
        "models", state_with_model
    )
    model_state = state_with_model.models[("models", "TestModel")]
    indexes = model_state.get_option_list("indexes")
    assert len(indexes) == 1
    assert indexes[0].name == "idx_renamed"
    assert indexes[0].field_names == ["id"]


def test_add_constraint_unnamed_is_kept_with_the_constraints(state_with_model: State):
    constraint = UniqueConstraint(fields=("id",))
    AddConstraint(model_name="TestModel", constraint=constraint).state_forward("models", state_with_model)
    model_state = state_with_model.models[("models", "TestModel")]
    assert model_state.get_option_list("constraints") == [constraint]


def test_remove_constraint_by_fields_success(state_with_model: State):
    AddConstraint(model_name="TestModel", constraint=UniqueConstraint(fields=("id",))).state_forward(
        "models", state_with_model
    )
    RemoveConstraint(model_name="TestModel", fields=["id"]).state_forward("models", state_with_model)
    model_state = state_with_model.models[("models", "TestModel")]
    assert model_state.get_option_list("constraints") == []


# --- describe() - every operation's human-readable summary, used in --dry-run/sqlmigrate
# output (Migration.apply()'s own collected_sql path) ---


def test_describe_index_operations():
    index = Index(fields=("id",), name="idx_id")
    assert AddIndex(model_name="TestModel", index=index).describe() == "Add index idx_id to TestModel"
    assert AddIndex(model_name="TestModel", index=Index(fields=("id",))).describe() == "Add index to TestModel"
    assert RemoveIndex(model_name="TestModel", name="idx_id").describe() == "Remove index idx_id from TestModel"
    assert RemoveIndex(model_name="TestModel", fields=["id"]).describe() == "Remove index on id from TestModel"
    assert (
        RenameIndex(model_name="TestModel", new_name="idx_renamed", old_name="idx_id").describe()
        == "Rename index idx_id to idx_renamed on TestModel"
    )


def test_describe_constraint_operations():
    constraint = UniqueConstraint(fields=("id",), name="uq_id")
    assert (
        AddConstraint(model_name="TestModel", constraint=constraint).describe() == "Add constraint uq_id to TestModel"
    )
    assert (
        RemoveConstraint(model_name="TestModel", name="uq_id").describe() == "Remove constraint uq_id from TestModel"
    )
    assert (
        RenameConstraint(model_name="TestModel", old_name="uq_id", new_name="uq_id_renamed").describe()
        == "Rename constraint uq_id to uq_id_renamed on TestModel"
    )


def test_describe_trigger_operations():
    trigger = Trigger(name="trg_id", on="INSERT", body=RawSQLTerm("SELECT 1;"))
    assert AddTrigger(model_name="TestModel", trigger=trigger).describe() == "Add trigger trg_id to TestModel"
    assert RemoveTrigger(model_name="TestModel", name="trg_id").describe() == "Remove trigger trg_id from TestModel"
    assert AlterTrigger(model_name="TestModel", trigger=trigger).describe() == "Alter trigger trg_id on TestModel"
    assert (
        RenameTrigger(model_name="TestModel", old_name="trg_id", new_name="trg_renamed").describe()
        == "Rename trigger trg_id to trg_renamed on TestModel"
    )


# --- state_editor=None no-op guard - real, reachable path: Migration.apply()/unapply() pass
# schema_editor=None whenever dry_run is True or no real editor was constructed. One
# representative operation per module (the guard's own code shape - "if not state_editor:
# return" - is identical across every operation in each file). ---


@pytest.mark.asyncio
async def test_add_index_database_forward_backward_noop_without_state_editor(state_with_model: State):
    index = Index(fields=("id",), name="idx_id")
    op = AddIndex(model_name="TestModel", index=index)
    await op.database_forward("models", state_with_model, state_with_model, state_editor=None)
    await op.database_backward("models", state_with_model, state_with_model, state_editor=None)


@pytest.mark.asyncio
async def test_add_constraint_database_forward_backward_noop_without_state_editor(state_with_model: State):
    constraint = UniqueConstraint(fields=("id",), name="uq_id")
    op = AddConstraint(model_name="TestModel", constraint=constraint)
    await op.database_forward("models", state_with_model, state_with_model, state_editor=None)
    await op.database_backward("models", state_with_model, state_with_model, state_editor=None)


@pytest.mark.asyncio
async def test_add_trigger_database_forward_backward_noop_without_state_editor(state_with_model: State):
    trigger = Trigger(name="trg_id", on="INSERT", body=RawSQLTerm("SELECT 1;"))
    op = AddTrigger(model_name="TestModel", trigger=trigger)
    await op.database_forward("models", state_with_model, state_with_model, state_editor=None)
    await op.database_backward("models", state_with_model, state_with_model, state_editor=None)


# --- AddIndex/AddConstraint state_forward - not guarded, just confirm real round-trip for
# RemoveIndex/RemoveConstraint/RenameConstraint's success paths, which the guard tests above
# don't otherwise exercise ---


def test_remove_index_success_then_missing(state_with_model: State):
    index = Index(fields=("id",), name="idx_id")
    AddIndex(model_name="TestModel", index=index).state_forward("models", state_with_model)
    RemoveIndex(model_name="TestModel", name="idx_id").state_forward("models", state_with_model)
    with pytest.raises(IncompatibleStateError):
        RemoveIndex(model_name="TestModel", name="idx_id").state_forward("models", state_with_model)


def test_rename_constraint_success(state_with_model: State):
    constraint = UniqueConstraint(fields=("id",), name="uq_id")
    AddConstraint(model_name="TestModel", constraint=constraint).state_forward("models", state_with_model)
    RenameConstraint(model_name="TestModel", old_name="uq_id", new_name="uq_id_renamed").state_forward(
        "models", state_with_model
    )
    model_state = state_with_model.models[("models", "TestModel")]
    names = {c.name for c in model_state.options["constraints"]}
    assert names == {"uq_id_renamed"}


def test_rename_constraint_check_constraint_success(state_with_model: State):
    constraint = CheckConstraint(check=RawSQLTerm("id > 0"), name="chk_id")
    AddConstraint(model_name="TestModel", constraint=constraint).state_forward("models", state_with_model)
    RenameConstraint(model_name="TestModel", old_name="chk_id", new_name="chk_id_renamed").state_forward(
        "models", state_with_model
    )
    model_state = state_with_model.models[("models", "TestModel")]
    names = {c.name for c in model_state.options["constraints"]}
    assert names == {"chk_id_renamed"}


# --- RunSQL: list/tuple forms, ValueError on bad tuple shape, noop sentinel ---


@pytest.mark.asyncio
async def test_run_sql_list_of_plain_strings():
    client = FakeClient("sql")
    editor = _TestSchemaEditor(client)
    state = State(models={}, apps=StateApps())

    op = RunSQL(["CREATE INDEX a ON t (x)", "CREATE INDEX b ON t (y)"])
    await op.run("models", state, dry_run=False, state_editor=editor)

    assert client.executed == ["CREATE INDEX a ON t (x)", "CREATE INDEX b ON t (y)"]


@pytest.mark.asyncio
async def test_run_sql_parameterized_tuples_collect_sql():
    client = FakeClient("sql")
    editor = _TestSchemaEditor(client, collect_sql=True)
    state = State(models={}, apps=StateApps())

    op = RunSQL([("INSERT INTO t (x) VALUES (%s)", [1])])
    await op.run("models", state, dry_run=False, state_editor=editor)

    assert editor.collected_sql == ["INSERT INTO t (x) VALUES (%s)  -- params: [1]"]


@pytest.mark.asyncio
async def test_run_sql_bad_tuple_shape_raises_value_error():
    client = FakeClient("sql")
    editor = _TestSchemaEditor(client, collect_sql=True)
    state = State(models={}, apps=StateApps())

    op = RunSQL([("sql", "params", "extra")])
    with pytest.raises(ConfigurationError, match="Expected a 2-tuple"):
        await op.run("models", state, dry_run=False, state_editor=editor)


@pytest.mark.asyncio
async def test_run_sql_noop_sentinel_executes_nothing():
    client = FakeClient("sql")
    editor = _TestSchemaEditor(client)
    state = State(models={}, apps=StateApps())

    op = RunSQL(RunSQL.noop)
    await op.run("models", state, dry_run=False, state_editor=editor)

    assert client.executed == []


@pytest.mark.asyncio
async def test_run_sql_backward_without_reverse_sql_raises():
    client = FakeClient("sql")
    editor = _TestSchemaEditor(client)
    state = State(models={}, apps=StateApps())

    op = RunSQL("CREATE INDEX a ON t (x)")
    with pytest.raises(NotImplementedError):
        await op.database_backward("models", state, state, state_editor=editor)


# --- SQLOperation (used internally by the autodetector for collected raw SQL) ---


@pytest.mark.asyncio
async def test_sql_operation_without_values():
    client = FakeClient("sql")
    editor = _TestSchemaEditor(client)
    state = State(models={}, apps=StateApps())

    op = SQLOperation("CREATE INDEX a ON t (x)", values=[])
    await op.run("models", state, dry_run=False, state_editor=editor)

    assert client.executed == ["CREATE INDEX a ON t (x)"]


@pytest.mark.asyncio
async def test_sql_operation_with_values_collect_sql():
    client = FakeClient("sql")
    editor = _TestSchemaEditor(client, collect_sql=True)
    state = State(models={}, apps=StateApps())

    op = SQLOperation("INSERT INTO t (x) VALUES ($1)", values=[1])
    await op.run("models", state, dry_run=False, state_editor=editor)

    assert editor.collected_sql == ["INSERT INTO t (x) VALUES ($1)  -- params: [1]"]


def test_sql_operation_state_forward_leaves_state_untouched(state_with_model: State):
    models_before = dict(state_with_model.models)

    SQLOperation("CREATE INDEX a ON t (x)", values=[]).state_forward("models", state_with_model)

    assert state_with_model.models == models_before


@pytest.mark.asyncio
async def test_sql_operation_applies_through_a_migration():
    """Migration.apply() calls state_forward() and database_forward() - SQLOperation used to
    raise NotImplementedError from both, so it could never run inside a real migration."""
    from hare.migrations.migration import Migration

    client = FakeClient("sql")
    editor = _TestSchemaEditor(client)
    migration = Migration("0001_initial", "models")
    migration.operations = [SQLOperation("CREATE INDEX a ON t (x)", values=[])]

    await migration.apply(State(models={}, apps=StateApps()), schema_editor=editor)

    assert client.executed == ["CREATE INDEX a ON t (x)"]


@pytest.mark.asyncio
async def test_sql_operation_is_not_reversible():
    from hare.migrations.migration import Migration

    client = FakeClient("sql")
    editor = _TestSchemaEditor(client)
    state = State(models={}, apps=StateApps())
    operation = SQLOperation("CREATE INDEX a ON t (x)", values=[])
    migration = Migration("0001_initial", "models")
    migration.operations = [operation]

    assert operation.reversible is False
    with pytest.raises(IrreversibleMigrationError, match="is not reversible"):
        await migration.unapply(state, schema_editor=editor)
    with pytest.raises(NotImplementedError, match="SQLOperation is not reversible"):
        await operation.database_backward("models", state, state, state_editor=editor)


# --- RunPython.database_backward ---


@pytest.mark.asyncio
async def test_run_python_backward_executes_reverse_code():
    calls = []

    def forward(apps, schema_editor):
        calls.append("forward")

    def backward(apps, schema_editor):
        calls.append("backward")

    client = FakeClient("sql")
    editor = _TestSchemaEditor(client)
    state = State(models={}, apps=StateApps())

    op = RunPython(forward, backward)
    await op.database_backward("models", state, state, state_editor=editor)

    assert calls == ["backward"]


@pytest.mark.asyncio
async def test_run_python_backward_without_reverse_code_raises():
    client = FakeClient("sql")
    editor = _TestSchemaEditor(client)
    state = State(models={}, apps=StateApps())

    op = RunPython(RunPython.noop)
    with pytest.raises(NotImplementedError):
        await op.database_backward("models", state, state, state_editor=editor)
