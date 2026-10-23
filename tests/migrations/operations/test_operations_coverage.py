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
from hare.migrations.exceptions import IncompatibleStateError
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
    ValidateConstraint,
)
from hare.migrations.state.state import State
from tests.utils.fake_client import FakeClient
from tests.utils.operations import apply_operations

pytestmark = pytest.mark.database_independent


class TestSchemaEditorTableComments(TableComments):
    __test__ = False

    def get_table_comment_sql(self, table: str, comment: str) -> str:
        return ""

    def get_column_comment_sql(self, table: str, column: str, comment: str) -> str:
        return ""


class _TestSchemaEditor(BaseSchemaEditor):
    table_comments_class = TestSchemaEditorTableComments


# --- IncompatibleStateError guards, one per operation type ---


@pytest.mark.parametrize(
    "make_operation",
    [
        pytest.param(
            lambda: RenameModel(old_name="Ghost", new_name="Renamed"), id="rename_model_missing_source_raises"
        ),
        pytest.param(lambda: RemoveField(model_name="TestModel", name="ghost"), id="remove_field_missing_raises"),
        pytest.param(
            lambda: RenameField(model_name="TestModel", old_name="ghost", new_name="renamed"),
            id="rename_field_missing_source_raises",
        ),
        pytest.param(lambda: RemoveIndex(model_name="TestModel", name="ghost_idx"), id="remove_index_missing_raises"),
        pytest.param(
            lambda: RenameIndex(model_name="TestModel", new_name="renamed_idx", old_name="ghost_idx"),
            id="rename_index_missing_old_name_raises",
        ),
        pytest.param(
            lambda: RemoveConstraint(model_name="TestModel", name="ghost"), id="remove_constraint_missing_raises"
        ),
        pytest.param(
            lambda: RenameConstraint(model_name="TestModel", old_name="ghost", new_name="renamed"),
            id="rename_constraint_missing_old_name_raises",
        ),
        pytest.param(
            lambda: RemoveTrigger(model_name="TestModel", name="ghost_trig"), id="remove_trigger_missing_raises"
        ),
        pytest.param(
            lambda: RenameTrigger(model_name="TestModel", old_name="ghost_trig", new_name="renamed_trig"),
            id="rename_trigger_missing_old_name_raises",
        ),
    ],
)
def test_an_operation_on_something_missing_raises(state_with_model: State, make_operation):
    with pytest.raises(IncompatibleStateError):
        make_operation().state_forward("models", state_with_model)


def test_add_field_already_present_raises(state_with_model: State):
    with pytest.raises(IncompatibleStateError):
        AddField(model_name="TestModel", name="id", field=fields.IntField(primary_key=True)).state_forward(
            "models", state_with_model
        )


def test_alter_field_missing_raises(state_with_model: State):
    with pytest.raises(IncompatibleStateError):
        AlterField(model_name="TestModel", name="ghost", field=fields.TextField()).state_forward(
            "models", state_with_model
        )


def test_removing_the_primary_key_raises_before_any_sql(state_with_model: State):
    """PostgreSQL dropped the key column, SQLite remade the table keeping it, ClickHouse failed partway."""
    with pytest.raises(ConfigurationError, match="still the model's primary key"):
        RemoveField(model_name="TestModel", name="id").state_forward("models", state_with_model)
    assert "id" in state_with_model.models[("models", "TestModel")].fields


def test_validating_a_declared_constraint_other_than_a_check_raises(state_with_model: State):
    """PostgreSQL refused it after the SQL ran ("cannot validate constraint"), SQLite and ClickHouse took it."""
    AddField(model_name="TestModel", name="code", field=fields.IntField()).state_forward("models", state_with_model)
    AddConstraint(
        model_name="TestModel", constraint=UniqueConstraint(fields=("code",), name="uid_code")
    ).state_forward("models", state_with_model)
    with pytest.raises(ConfigurationError, match="'uid_code' on TestModel is a UniqueConstraint"):
        ValidateConstraint(model_name="TestModel", name="uid_code").state_forward("models", state_with_model)
    # A name the state doesn't know - a relation's foreign key, a RunSQL's constraint - is the database's.
    ValidateConstraint(model_name="TestModel", name="fk_added_by_hand").state_forward("models", state_with_model)


def test_rename_field_target_already_present_raises(state_with_model: State):
    AddField(model_name="TestModel", name="name", field=fields.TextField()).state_forward("models", state_with_model)
    with pytest.raises(IncompatibleStateError):
        RenameField(model_name="TestModel", old_name="id", new_name="name").state_forward("models", state_with_model)


def test_alter_trigger_missing_raises(state_with_model: State):
    trigger = Trigger(name="ghost_trig", on="INSERT", body=RawSQLTerm("SELECT 1;"))
    with pytest.raises(IncompatibleStateError):
        AlterTrigger(model_name="TestModel", trigger=trigger).state_forward("models", state_with_model)


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


@pytest.mark.parametrize("operation_class", [RenameIndex, RenameConstraint, RenameTrigger])
def test_a_rename_to_an_empty_name_is_refused_when_the_migration_loads(operation_class):
    """The migration loaded and failed when applied, with "Index None is not present on Doc"."""
    with pytest.raises(ConfigurationError, match="new_name takes a name, got ''"):
        operation_class(model_name="TestModel", new_name="", old_name="old_one")


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


@pytest.mark.parametrize(
    "make_operation",
    [
        pytest.param(
            lambda: AddIndex(model_name="TestModel", index=Index(fields=("id",), name="idx_id")),
            id="add_index_database_forward_backward_noop_without_state_editor",
        ),
        pytest.param(
            lambda: AddConstraint(model_name="TestModel", constraint=UniqueConstraint(fields=("id",), name="uq_id")),
            id="add_constraint_database_forward_backward_noop_without_state_editor",
        ),
        pytest.param(
            lambda: AddTrigger(
                model_name="TestModel", trigger=Trigger(name="trg_id", on="INSERT", body=RawSQLTerm("SELECT 1;"))
            ),
            id="add_trigger_database_forward_backward_noop_without_state_editor",
        ),
    ],
)
@pytest.mark.asyncio
async def test_database_forward_backward_noop_without_state_editor(state_with_model: State, make_operation):
    op = make_operation()
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
    state = State()

    op = RunSQL(["CREATE INDEX a ON t (x)", "CREATE INDEX b ON t (y)"])
    await apply_operations("models", state, op, schema_editor=editor)

    assert client.executed == ["CREATE INDEX a ON t (x)", "CREATE INDEX b ON t (y)"]


@pytest.mark.asyncio
async def test_run_sql_parameterized_tuples_collect_sql():
    client = FakeClient("sql")
    editor = _TestSchemaEditor(client, collect_sql=True)
    state = State()

    op = RunSQL([("INSERT INTO t (x) VALUES (%s)", [1])])
    await apply_operations("models", state, op, schema_editor=editor)

    assert editor.collected_sql == ["INSERT INTO t (x) VALUES (1)"]


@pytest.mark.asyncio
async def test_run_sql_bad_tuple_shape_raises_value_error():
    client = FakeClient("sql")
    editor = _TestSchemaEditor(client, collect_sql=True)
    state = State()

    op = RunSQL([("sql", "params", "extra")])
    with pytest.raises(ConfigurationError, match="Expected a 2-tuple"):
        await apply_operations("models", state, op, schema_editor=editor)


@pytest.mark.asyncio
async def test_run_sql_noop_sentinel_executes_nothing():
    client = FakeClient("sql")
    editor = _TestSchemaEditor(client)
    state = State()

    op = RunSQL(RunSQL.noop)
    await apply_operations("models", state, op, schema_editor=editor)

    assert client.executed == []


@pytest.mark.asyncio
async def test_run_sql_backward_without_reverse_sql_raises():
    client = FakeClient("sql")
    editor = _TestSchemaEditor(client)
    state = State()

    op = RunSQL("CREATE INDEX a ON t (x)")
    with pytest.raises(NotImplementedError):
        await op.database_backward("models", state, state, state_editor=editor)


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
    state = State()

    op = RunPython(forward, backward)
    await op.database_backward("models", state, state, state_editor=editor)

    assert calls == ["backward"]


@pytest.mark.asyncio
async def test_run_python_backward_without_reverse_code_raises():
    client = FakeClient("sql")
    editor = _TestSchemaEditor(client)
    state = State()

    op = RunPython(RunPython.noop)
    with pytest.raises(NotImplementedError):
        await op.database_backward("models", state, state, state_editor=editor)
