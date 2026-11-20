"""generate_schemas() and the migrations of the same models build the same schema: every table read back
from the catalog - its columns, keys, foreign keys, indexes and constraints - is the same either way."""

import dataclasses
from typing import Any

import pytest

from hare import Connections, Model, fields
from hare.contrib.test.databases.temporary_databases import TemporaryDatabases
from hare.core.hare_context import HareContext
from hare.ddl import RawSQLTerm, View
from hare.ddl.constraints import CheckConstraint
from hare.exceptions import IntegrityError
from hare.inspectdb.introspection.column_info import ColumnInfo
from hare.inspectdb.introspection.composite_foreign_key_info import CompositeForeignKeyInfo
from hare.inspectdb.introspection.database_catalog import DatabaseCatalog
from hare.inspectdb.introspection.index_info import IndexInfo
from hare.inspectdb.introspection.table_info import TableInfo
from hare.migrations.autodetection.migration_splitter import MigrationSplitter
from hare.migrations.autodetection.operation_generator import OperationGenerator
from hare.migrations.migration import Migration
from hare.migrations.operations import AlterField, RenameView
from hare.migrations.state.state import State
from hare.query.expressions import Q
from hare.sql.order import Order
from tests.utils.database_under_test import DatabaseUnderTest

MODULES = ["tests.shared_models.testmodels"]


def get_comparable(value: Any) -> Any:
    """Ignore catalog collection order while preserving order inside schema objects."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        result = {field.name: get_comparable(getattr(value, field.name)) for field in dataclasses.fields(value)}
        if isinstance(value, TableInfo):
            for name in (
                "diagnostics",
                "columns",
                "indexes",
                "column_indexes",
                "triggers",
                "exclusion_constraints",
                "composite_foreign_keys",
                "check_constraints",
                "not_valid_constraint_names",
                "policies",
                "grants",
                "sequences",
                "sequences_of_other_tables",
            ):
                result[name] = sorted(result[name], key=repr)
        return result
    if isinstance(value, dict):
        return {key: get_comparable(item) for key, item in value.items()}
    if isinstance(value, set):
        return sorted((get_comparable(item) for item in value), key=repr)
    if isinstance(value, (list, tuple)):
        return [get_comparable(item) for item in value]
    return value


@pytest.mark.database_independent
@pytest.mark.parametrize(
    ("before", "after"),
    [
        pytest.param(IndexInfo(["left", "right"], False), IndexInfo(["right", "left"], False), id="index-columns"),
        pytest.param(
            IndexInfo(["left", "right"], False, key_orders=[Order.ASC_NULLS_LAST, Order.DESC_NULLS_FIRST]),
            IndexInfo(["left", "right"], False, key_orders=[Order.DESC_NULLS_FIRST, Order.ASC_NULLS_LAST]),
            id="index-directions",
        ),
        pytest.param(
            ColumnInfo("status", "ticket_status", False, False, False, enum_labels=("open", "closed")),
            ColumnInfo("status", "ticket_status", False, False, False, enum_labels=("closed", "open")),
            id="enum-labels",
        ),
        pytest.param(
            CompositeForeignKeyInfo("target", ("left", "right"), "parent"),
            CompositeForeignKeyInfo("target", ("right", "left"), "parent"),
            id="foreign-key-columns",
        ),
    ],
)
def test_schema_comparison_preserves_ordered_properties(before, after):
    assert get_comparable(before) != get_comparable(after)


@pytest.mark.database_independent
def test_schema_comparison_ignores_catalog_collection_order():
    columns = [
        ColumnInfo("id", "INTEGER", False, True, True),
        ColumnInfo("name", "TEXT", True, False, False),
    ]
    indexes = [IndexInfo(["id"], True, name="first"), IndexInfo(["name"], False, name="second")]
    before = TableInfo("record", columns=columns, indexes=indexes)
    after = TableInfo("record", columns=list(reversed(columns)), indexes=list(reversed(indexes)))
    assert get_comparable(before) == get_comparable(after)


async def read_schema(connection, models) -> dict[tuple[str | None, str], Any]:
    introspector_class = DatabaseCatalog.get_introspector_class(connection)
    default_schema = await introspector_class.fetch_default_schema(connection)
    schemas = {
        default_schema,
        *(
            HareContext.require_current().apps.get_binding(model).schema
            for model in models
            if HareContext.require_current().apps.get_binding(model).schema
        ),
    }
    tables: dict[tuple[str | None, str], Any] = {}
    for schema in sorted(schemas):
        table_names = await introspector_class.fetch_table_names(connection, schema, True)
        for table_info in await introspector_class.fetch_tables(connection, table_names, schema):
            tables[(schema, table_info.name)] = get_comparable(table_info)
    return tables


@pytest.mark.asyncio
async def test_postgresql_generic_models_have_the_same_generated_and_migrated_schema():
    if DatabaseUnderTest.get_dialect().name != "postgresql":
        pytest.skip("PostgreSQL-specific generic model graph; SQLite is covered by the model roundtrip")

    async with DatabaseUnderTest.open_model_context(MODULES, generate_schemas=True):
        connection = Connections.get("models")
        editor = connection.dialect.schema_editor_class(connection)
        models = [binding._model for binding in editor.table_creation.get_bindings_to_create()]
        generated_schema = await read_schema(connection, models)

    async with DatabaseUnderTest.open_model_context(MODULES, generate_schemas=False):
        connection = Connections.get("models")
        editor = connection.dialect.schema_editor_class(connection)
        models = [binding._model for binding in editor.table_creation.get_bindings_to_create()]
        new_state = State.from_models(models)
        operations_by_app = {
            app_label: OperationGenerator(State(), new_state).generate(app_labels=[app_label])
            for app_label in sorted({HareContext.require_current().apps.get_binding(model).app for model in models})
        }
        split_migrations = MigrationSplitter(State(), list(operations_by_app), lambda app_label: []).split(
            operations_by_app
        )
        state = State()
        for position, split_migration in enumerate(split_migrations):
            migration = Migration(f"{position:04d}", split_migration.app_label, operations=split_migration.operations)
            state = await migration.apply(state, schema_editor=editor)
        migrated_schema = await read_schema(connection, models)

    assert sorted(generated_schema) == sorted(migrated_schema)
    for table_key, generated_table in generated_schema.items():
        assert generated_table == migrated_schema[table_key], table_key


@pytest.mark.asyncio
async def test_a_safe_schema_script_runs_again_over_the_schema_it_built():
    async with DatabaseUnderTest.open_model_context(MODULES, generate_schemas=True):
        connection = Connections.get("models")
        script = connection.get_schema_sql(safe=True)
        await connection.execute_script(script)
        await connection.execute_script(script)


@pytest.mark.asyncio
@pytest.mark.parametrize("create_directly", [True, False], ids=["generate-schemas", "migrate"])
@pytest.mark.parametrize("qualified", [False, True], ids=["default-schema", "qualified"])
async def test_created_views_and_constraints_work_and_support_the_next_migration(create_directly, qualified):
    if DatabaseUnderTest.get_dialect().name != "postgresql":
        pytest.skip("This case uses PostgreSQL schemas, constraints and ALTER VIEW")
    schema_name = "activity_archive" if qualified else None
    prefix = f"{schema_name}." if schema_name else ""

    def saved_entries(table):
        return fields.ManyToManyField(
            "models.Entry", related_name="saved_by", through=table, forward_key="entry_id", backward_key="account_id"
        )

    class Account(Model):
        id = fields.IntField(primary_key=True, generated=False)
        saved = saved_entries("parity_saved_entries")

        class Meta:
            schema = schema_name
            table = "parity_account"

    class Entry(Model):
        id = fields.IntField(primary_key=True, generated=False)
        account = fields.ForeignKeyField(Account, related_name="entries")
        amount = fields.IntField()

        class Meta:
            schema = schema_name
            table = "parity_entry"
            constraints = [CheckConstraint(name="positive_parity_amount", check=Q(amount__gt=0))]
            views = [
                View(
                    "parity_large",
                    query=RawSQLTerm(f"SELECT account_id, amount FROM {prefix}parity_entry WHERE amount > 4"),
                )
            ]

    config = {"connections": {"models": None}, "apps": {}}
    async with TemporaryDatabases(config, DatabaseUnderTest.get_url(), generate_schemas=False) as context:
        connection = context.get_connection()
        if schema_name:
            await connection.execute_script("CREATE SCHEMA activity_archive")
        context.register_live_models([Account, Entry], "models", connection_alias="models")
        desired = State.from_models([Account, Entry], app_label="models", default_connections={"models": "models"})
        editor = connection.dialect.schema_editor_class(connection)
        if create_directly:
            await context.generate_schemas()
            before = desired
        else:
            empty = State(default_connections={"models": "models"})
            operations = OperationGenerator(empty, desired).generate()
            before = await Migration("initial", "models", operations=operations).apply(empty, schema_editor=editor)
        await connection.execute_script(
            f"INSERT INTO {prefix}parity_account VALUES (1), (2); "
            f"INSERT INTO {prefix}parity_entry VALUES (1, 1, 5), (2, 2, 3)"
        )
        account = await Account.objects.get(id=1)
        await account.saved.add(await Entry.objects.get(id=2))
        saved = [{"account_id": 1, "entry_id": 2}]
        assert (
            await connection.execute_dicts(f"SELECT account_id, entry_id FROM {prefix}parity_saved_entries") == saved
        )
        for insert in ((1, 2), (1, 999)):
            with pytest.raises(IntegrityError):
                async with connection._in_transaction() as transaction:
                    await transaction.execute(
                        f"INSERT INTO {prefix}parity_saved_entries(account_id, entry_id) VALUES ($1, $2)", list(insert)
                    )
        with pytest.raises(IntegrityError):
            async with connection._in_transaction() as transaction:
                await transaction.execute(f"INSERT INTO {prefix}parity_entry VALUES (3, 1, 0)")
        assert await connection.execute_dicts(f"SELECT account_id, amount FROM {prefix}parity_large") == [
            {"account_id": 1, "amount": 5}
        ]
        move = Migration(
            "rename_view_and_saved_entries",
            "models",
            operations=[
                RenameView("Entry", "parity_large", "parity_report"),
                AlterField("Account", "saved", saved_entries("parity_archived_entries")),
            ],
        )
        await move.apply(before, schema_editor=editor)
        assert await connection.execute_dicts(f"SELECT account_id, amount FROM {prefix}parity_report") == [
            {"account_id": 1, "amount": 5}
        ]
        assert await connection.execute_dicts(f"SELECT id FROM {prefix}parity_account ORDER BY id") == [
            {"id": 1},
            {"id": 2},
        ]
        assert "parity_saved_entries" not in await DatabaseCatalog.get_table_names(connection, schema_name)
        assert (
            await connection.execute_dicts(f"SELECT account_id, entry_id FROM {prefix}parity_archived_entries")
            == saved
        )
        await move.unapply(before, schema_editor=editor)
        assert await connection.execute_dicts(f"SELECT account_id, amount FROM {prefix}parity_large") == [
            {"account_id": 1, "amount": 5}
        ]
        assert "parity_archived_entries" not in await DatabaseCatalog.get_table_names(connection, schema_name)
        assert (
            await connection.execute_dicts(f"SELECT account_id, entry_id FROM {prefix}parity_saved_entries") == saved
        )
