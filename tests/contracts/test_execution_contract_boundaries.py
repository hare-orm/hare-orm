"""Shared execution contracts across object scopes, field writers and migration containers."""

from copy import copy
from types import SimpleNamespace

import pytest

from hare import fields
from hare.ddl.enums import SchemaPartType
from hare.dialects.clickhouse.fields.low_cardinality_field import LowCardinalityField
from hare.dialects.clickhouse.fields.variant_field import VariantField
from hare.dialects.dialect_registry import DialectRegistry
from hare.dialects.postgresql.query import PostgresqlQuery
from hare.dialects.sqlite.query import SqliteQuery
from hare.exceptions import ValidationError
from hare.migrations.execution.migration_runner import MigrationRunner
from hare.migrations.operations import CreateModel, RemoveField, SeparateDatabaseAndState
from hare.migrations.state.prepared_operation_states import PreparedOperationStates
from hare.migrations.state.state import State
from hare.models.class_building.generic_foreign_keys import GenericForeignKeys
from hare.sql import Table
from hare.transactions.enums import TransactionOutcome
from hare.transactions.transaction_group import TransactionGroup
from hare.transactions.transaction_group_exit import TransactionGroupExit
from tests.utils.fake_client import FakeClient

pytestmark = pytest.mark.database_independent


@pytest.mark.parametrize(
    ("dialect_name", "object_type", "other_table", "same_scope"),
    [
        ("postgresql", SchemaPartType.INDEX, "second", True),
        ("postgresql", SchemaPartType.TRIGGER, "second", False),
        ("sqlite", SchemaPartType.INDEX, "second", True),
        ("sqlite", SchemaPartType.TRIGGER, "second", True),
        ("clickhouse", SchemaPartType.INDEX, "second", False),
        ("clickhouse", SchemaPartType.VIEW, "second", True),
    ],
)
def test_object_names_follow_the_database_namespace(dialect_name, object_type, other_table, same_scope):
    literals = DialectRegistry.get_dialect(dialect_name).literals
    first = literals.get_schema_object_key(object_type, None, "first", "shared_name")
    second = literals.get_schema_object_key(object_type, None, other_table, "shared_name")
    assert first is not None
    assert (first == second) is same_scope


@pytest.mark.parametrize("object_type", [SchemaPartType.TABLE, SchemaPartType.INDEX, SchemaPartType.VIEW])
def test_sqlite_default_schema_and_case_share_a_physical_identity(object_type):
    literals = DialectRegistry.get_dialect("sqlite").literals
    assert literals.get_schema_object_key(object_type, None, "item", "Example") == literals.get_schema_object_key(
        object_type, "MAIN", "ITEM", "example"
    )


@pytest.mark.parametrize("dialect_name", ["postgresql", "sqlite", "clickhouse"])
def test_function_declarations_are_not_compared_by_bare_name(dialect_name):
    literals = DialectRegistry.get_dialect(dialect_name).literals
    assert literals.get_schema_object_key(SchemaPartType.FUNCTION, None, "item", "convert") is None


@pytest.mark.parametrize("field_type", ["low_cardinality", "variant"])
@pytest.mark.parametrize("write_type", ["db", "lookup"])
def test_prepared_wrappers_validate_the_outer_field_once(field_type, write_type):
    calls = []

    def check(value):
        calls.append(value)
        if value == "refused":
            raise ValidationError("outer validator refused the value")

    field = (
        LowCardinalityField(fields.TextField(), validators=[check])
        if field_type == "low_cardinality"
        else VariantField([fields.IntField(), fields.TextField()], validators=[check])
    )
    types = DialectRegistry.get_dialect("clickhouse").types
    writer = types.get_db_writer(field) if write_type == "db" else types.get_lookup_writer(field)
    writer("accepted", None)
    assert calls == ["accepted"]
    with pytest.raises(ValidationError, match="outer validator"):
        writer("refused", None)
    assert calls == ["accepted", "refused"]
    if write_type == "lookup":
        assert writer(None, None) is None


@pytest.mark.parametrize("query_class", [PostgresqlQuery, SqliteQuery])
def test_copy_detaches_a_mutable_source_before_its_next_change(query_class):
    table = Table("item")
    source = query_class.from_(table).select(table.id)
    source.immutable = False
    branch = copy(source)
    branch_sql = str(branch)
    source.select(table.name)
    assert str(branch) == branch_sql
    assert str(source) != branch_sql


@pytest.mark.parametrize("database_change", [False, True])
def test_nested_migration_effects_describe_only_physical_changes(database_change):
    state = State()
    CreateModel(
        name="Item", fields=[("id", fields.IntField(primary_key=True)), ("code", fields.TextField())]
    ).state_forward("shop", state)
    removal = RemoveField(model_name="Item", name="code")
    operation = SeparateDatabaseAndState(
        database_operations=[SeparateDatabaseAndState(database_operations=[removal])] if database_change else [],
        state_operations=[] if database_change else [removal],
    )
    prepared = PreparedOperationStates("shop", [operation], state)
    client = FakeClient("sqlite")
    editor = client.dialect.schema_editor_class(client, collect_sql=True)
    effect = MigrationRunner._get_prepared_effect("shop", prepared.operations[0], editor)
    assert effect.loses_data is database_change
    assert ("shop", "Item") in state.models
    assert "code" in state.models[("shop", "Item")].fields


@pytest.mark.parametrize("outcome", list(TransactionOutcome))
def test_a_group_allows_retries_only_after_confirmed_rollback(outcome):
    group = TransactionGroup(("first", "second"))
    group.exits = [
        TransactionGroupExit("first", False, None, TransactionOutcome.ROLLED_BACK),
        TransactionGroupExit("second", False, None, outcome),
    ]
    assert group.rolled_back is (outcome is TransactionOutcome.ROLLED_BACK)
    group.exits.pop()
    assert not group.rolled_back


def test_generic_relation_suffixes_preserve_branch_boundaries():
    fields_by_name = {
        "first": SimpleNamespace(branch_names=("north_east", "north")),
        "second": SimpleNamespace(branch_names=("south", "east_south")),
    }
    combinations = GenericForeignKeys.get_branch_combinations(("first", "second"), fields_by_name)
    assert len({suffix for suffix, _ in combinations}) == len(combinations)
    assert len({tuple(names) for _, names in combinations}) == len(combinations)
