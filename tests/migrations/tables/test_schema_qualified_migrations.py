"""Integration tests for schema-qualified table names in migration schema editors.

Covers:
- Schema-qualified DDL generation per dialect (collect_sql mode)
- CreateSchema / DropSchema operations
- Autodetector schema detection
- MigrationWriter serialization of schema operations
- through_schema query-time behavior (filters, joins, add/remove)
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from hare import fields
from hare.contrib.test import requires_features
from hare.ddl import DatabaseFunction, DatabaseSequence, MaterializedView, RawSQLTerm, View
from hare.ddl.enums import TriggerEvent
from hare.ddl.indexes import Index
from hare.ddl.schema_objects.trigger import Trigger
from hare.dialects.base.constants import SQL_DIALECT
from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
from hare.dialects.base.schema.tables.table_comments import TableComments
from hare.dialects.postgresql.schema.postgresql_schema_editor import PostgresqlSchemaEditor
from hare.dialects.sqlite.schema.sqlite_schema_editor import SqliteSchemaEditor
from hare.exceptions import DatabaseError
from hare.fields.relations.fields import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.migrations.autodetection.operation_generator import OperationGenerator
from hare.migrations.operations import (
    AlterModelSchema,
    CreateModel,
    CreateSchema,
    DeleteModel,
    DropSchema,
)
from hare.migrations.state.state import State
from hare.migrations.state.state_apps import StateApps
from hare.migrations.writer import MigrationWriter
from hare.models import Model
from hare.query.expressions import ExpressionContext, Q
from hare.query.lookup_info.lookup_paths import LookupPaths
from hare.query.queryset.relations.many_to_many_relation import ManyToManyRelation
from hare.sql import Table
from tests.utils.fake_client import FakeClient
from tests.utils.operations import apply_operations

pytestmark = pytest.mark.database_independent


def init_apps(*models: type[Model]) -> None:
    apps = StateApps()
    for model in models:
        apps.register_model("models", model)
    apps.init_relations()


# ---------------------------------------------------------------------------
# Test models
# ---------------------------------------------------------------------------


def _make_schema_models() -> tuple[type[Model], type[Model], type[Model], type[Model]]:
    """Build models with Meta.schema='custom' for testing."""

    class SchemaCategory(Model):
        id = fields.IntField(primary_key=True)
        name = fields.TextField()

        class Meta:
            app = "models"
            table = "category"
            schema = "custom"

    class SchemaProduct(Model):
        id = fields.IntField(primary_key=True)
        name = fields.TextField()
        category: ForeignKeyFieldInstance[Any] = fields.ForeignKeyField(
            "models.SchemaCategory", related_name="products"
        )

        class Meta:
            app = "models"
            table = "product"
            schema = "custom"

    class SchemaTag(Model):
        id = fields.IntField(primary_key=True)
        name = fields.TextField()

        class Meta:
            app = "models"
            table = "tag"
            schema = "custom"

    class SchemaProductWithTags(Model):
        id = fields.IntField(primary_key=True)
        name = fields.TextField()
        tags: ManyToManyRelation[Any] = fields.ManyToManyField("models.SchemaTag", related_name="products")

        class Meta:
            app = "models"
            table = "product"
            schema = "custom"

    init_apps(SchemaCategory, SchemaProduct)
    init_apps(SchemaTag, SchemaProductWithTags)
    return SchemaCategory, SchemaProduct, SchemaTag, SchemaProductWithTags


# ---------------------------------------------------------------------------
# 1. Schema-qualified DDL per dialect
# ---------------------------------------------------------------------------


class TestEditorTableComments(TableComments):
    __test__ = False

    def get_table_comment_sql(self, table: str, comment: str) -> str:
        return ""

    def get_column_comment_sql(self, table: str, column: str, comment: str) -> str:
        return ""


class _TestEditor(BaseSchemaEditor):
    table_comments_class = TestEditorTableComments

    """Minimal concrete editor for tests (base ANSI SQL dialect)."""


@pytest.mark.parametrize(
    ("method_name", "expected"),
    [
        pytest.param("create_model", 'CREATE TABLE "custom"."category"', id="base_create_model_with_schema"),
        pytest.param("delete_model", 'DROP TABLE "custom"."category"', id="base_delete_model_qualified"),
    ],
)
@pytest.mark.asyncio
async def test_base_editor_qualifies_the_table_with_its_schema(method_name, expected) -> None:
    SchemaCategory, *_ = _make_schema_models()
    client = FakeClient("sql")
    editor = _TestEditor(client)

    await getattr(editor, method_name)(SchemaCategory)

    sql = client.executed[0]
    assert expected in sql


@pytest.mark.asyncio
async def test_base_create_model_without_schema() -> None:
    """Without Meta.schema, output is unchanged (backward compatible)."""

    class PlainWidget(Model):
        id = fields.IntField(primary_key=True)
        name = fields.TextField()

        class Meta:
            app = "models"
            table = "widget"

    client = FakeClient("sql")
    editor = _TestEditor(client)
    await editor.create_model(PlainWidget)

    sql = client.executed[0]
    assert 'CREATE TABLE "widget"' in sql
    assert '"custom"' not in sql


@pytest.mark.asyncio
async def test_base_fk_references_qualified() -> None:
    """FK REFERENCES uses schema-qualified target table."""
    _, SchemaProduct, *_ = _make_schema_models()
    client = FakeClient("sql")
    editor = _TestEditor(client)

    await editor.create_model(SchemaProduct)

    sql = client.executed[0]
    assert 'REFERENCES "custom"."category"' in sql


@pytest.mark.asyncio
async def test_base_m2m_table_qualified() -> None:
    """M2M through table is schema-qualified."""
    *_, SchemaTag, SchemaProductWithTags = _make_schema_models()
    client = FakeClient("sql")
    editor = _TestEditor(client)

    await editor.create_model(SchemaProductWithTags)

    combined = "\n".join(client.executed)
    assert 'CREATE TABLE "custom"."product_tag"' in combined


@pytest.mark.asyncio
async def test_base_add_field_qualified() -> None:
    """ALTER TABLE ADD COLUMN uses schema-qualified name."""
    SchemaCategory, *_ = _make_schema_models()
    client = FakeClient("sql")
    editor = _TestEditor(client)

    await editor.add_field(SchemaCategory, SchemaCategory, "name")

    sql = client.executed[0]
    assert 'ALTER TABLE "custom"."category" ADD COLUMN' in sql


@pytest.mark.asyncio
async def test_base_rename_table_qualified() -> None:
    """RENAME TABLE names the table schema-qualified and its new name alone - the table stays
    in its schema, and RENAME TO takes no schema."""
    SchemaCategory, *_ = _make_schema_models()
    client = FakeClient("sql")
    editor = _TestEditor(client)

    await editor.rename_table(SchemaCategory, "category", "categories")

    sql = client.executed[0]
    assert sql == 'ALTER TABLE "custom"."category" RENAME TO "categories"'


@pytest.mark.asyncio
async def test_base_remove_index_qualified() -> None:
    """DROP INDEX has no ON/ALTER TABLE clause to carry the schema - unlike every other DDL
    statement here, the index name itself must be schema-qualified, or it silently targets
    whichever schema happens to be first on the connection's search_path instead of the
    model's own schema (a real risk once two schemas hold a same-named index, e.g. identical
    models in two tenant schemas)."""
    SchemaCategory, *_ = _make_schema_models()
    client = FakeClient("sql")
    editor = _TestEditor(client)

    await editor.remove_index(SchemaCategory, Index(fields=("name",), name="my_named_idx"))

    sql = client.executed[0]
    assert sql == 'DROP INDEX "custom"."my_named_idx"'


@pytest.mark.asyncio
async def test_base_rename_index_qualified() -> None:
    """ALTER INDEX ... RENAME TO also has no table clause - the old name must be
    schema-qualified; the new name stays a plain quoted name (RENAME can't move an index
    across schemas, only rename it within its current one)."""
    SchemaCategory, *_ = _make_schema_models()
    client = FakeClient("sql")
    editor = _TestEditor(client)

    await editor.rename_index(
        SchemaCategory,
        Index(fields=("name",), name="my_named_idx"),
        Index(fields=("name",), name="my_renamed_idx"),
    )

    assert client.executed[0] == 'DROP INDEX "custom"."my_named_idx"'
    assert client.executed[1].startswith('CREATE INDEX "my_renamed_idx" ON "custom"."category"')


# ---------------------------------------------------------------------------
# 2. Per-dialect schema qualification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_postgres_create_table_with_schema() -> None:
    SchemaCategory, *_ = _make_schema_models()
    client = FakeClient("postgresql", inline_comment=False)
    editor = PostgresqlSchemaEditor(client)

    await editor.create_model(SchemaCategory)

    sql = client.executed[0]
    assert 'CREATE TABLE "custom"."category"' in sql


@pytest.mark.asyncio
async def test_sqlite_names_the_attached_database_of_the_schema() -> None:
    """SQLite reads a schema as an attached database: the table is named in it."""
    SchemaCategory, *_ = _make_schema_models()
    client = FakeClient("sqlite", inline_comment=True)
    editor = SqliteSchemaEditor(client)

    await editor.create_model(SchemaCategory)

    sql = client.executed[0]
    assert 'CREATE TABLE "custom"."category"' in sql


# ---------------------------------------------------------------------------
# 3. CreateSchema / DropSchema operations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method_name", "expected"),
    [
        pytest.param("create_schema", 'CREATE SCHEMA IF NOT EXISTS "custom";', id="create_schema_postgres"),
        pytest.param("drop_schema", 'DROP SCHEMA IF EXISTS "custom";', id="drop_schema_postgres"),
    ],
)
@pytest.mark.asyncio
async def test_a_schema_statement_postgres(method_name, expected) -> None:
    client = FakeClient("postgresql", inline_comment=False)
    editor = PostgresqlSchemaEditor(client)

    await getattr(editor.schemas, method_name)("custom")

    assert client.executed == [expected]


@pytest.mark.asyncio
async def test_create_schema_noop_for_base(empty_state: State) -> None:
    """An editor with no schema support (e.g. base ANSI SQL, SQLite) is a silent no-op for
    CreateSchema - create_schema/drop_schema no longer exist on it at all, so the operation's own
    isinstance(state_editor, SchemaAndExtensionSupportMixin) guard is what skips the call."""
    client = FakeClient("sqlite")
    editor = _TestEditor(client)

    op = CreateSchema(schema_name="custom")
    await apply_operations("models", empty_state, op, schema_editor=editor)

    assert client.executed == []


@pytest.mark.asyncio
async def test_create_schema_and_drop_schema_noop_for_real_sqlite_editor(empty_state: State) -> None:
    """Same no-op guarantee as test_create_schema_noop_for_base, but against the REAL
    SqliteSchemaEditor production class (not the generic _TestEditor fake) - confirms
    SchemaAndExtensionSupportMixin genuinely isn't mixed into it (create_schema/drop_schema don't
    exist on it at all) and that CreateSchema/DropSchema's own isinstance guard correctly skips
    calling them instead of raising AttributeError."""
    client = FakeClient("sqlite", inline_comment=True)
    editor = SqliteSchemaEditor(client)

    create_op = CreateSchema(schema_name="custom")
    await apply_operations("models", empty_state, create_op, schema_editor=editor)
    drop_op = DropSchema(schema_name="custom")
    await apply_operations("models", empty_state, drop_op, schema_editor=editor)

    assert client.executed == []


@pytest.mark.parametrize(
    ("operation_class", "expected"),
    [
        pytest.param(CreateSchema, 'CREATE SCHEMA IF NOT EXISTS "custom";', id="create_schema_operation_runs"),
        pytest.param(DropSchema, 'DROP SCHEMA IF EXISTS "custom";', id="drop_schema_operation_runs"),
    ],
)
@pytest.mark.asyncio
async def test_a_schema_operation_runs(empty_state: State, operation_class, expected) -> None:
    """CreateSchema/DropSchema call schema_editor.create_schema()/drop_schema()."""
    client = FakeClient("postgresql", inline_comment=False)
    editor = PostgresqlSchemaEditor(client)

    op = operation_class(schema_name="custom")
    await apply_operations("models", empty_state, op, schema_editor=editor)

    assert expected in client.executed


@pytest.mark.asyncio
async def test_create_schema_operation_backward(empty_state: State) -> None:
    """CreateSchema backward calls drop_schema."""
    client = FakeClient("postgresql", inline_comment=False)
    editor = PostgresqlSchemaEditor(client)

    op = CreateSchema(schema_name="custom")
    old_state = empty_state.clone()
    op.state_forward("models", empty_state)
    await op.database_backward("models", old_state, empty_state, editor)

    assert 'DROP SCHEMA IF EXISTS "custom";' in client.executed


def test_create_schema_describe() -> None:
    op = CreateSchema(schema_name="custom")
    assert op.describe() == "Create schema custom"


def test_drop_schema_describe() -> None:
    op = DropSchema(schema_name="custom")
    assert op.describe() == "Drop schema custom"


# ---------------------------------------------------------------------------
# 4. Autodetector schema detection
# ---------------------------------------------------------------------------


def _build_state_with_schema_model(state: State, schema: str | None = None) -> None:
    """Add a model with optional schema to a state."""
    options: dict[str, Any] = {"table": "product", "app": "models"}
    if schema:
        options["schema"] = schema
    CreateModel(
        name="Product",
        fields=[("id", fields.IntField(primary_key=True))],
        options=options,
    ).state_forward("models", state)


def test_autodetector_generates_create_schema() -> None:
    """When a model with new schema appears, CreateSchema is emitted."""
    old_state = State()
    new_state = State()
    _build_state_with_schema_model(new_state, schema="custom")

    ops = OperationGenerator(old_state, new_state).generate()

    assert len(ops) >= 2
    assert isinstance(ops[0], CreateSchema)
    assert ops[0].schema_name == "custom"
    assert isinstance(ops[1], CreateModel)


def test_autodetector_never_drops_a_schema() -> None:
    """A schema no model uses any more stays - it may hold other objects."""
    old_state = State()
    new_state = State()
    _build_state_with_schema_model(old_state, schema="custom")

    ops = OperationGenerator(old_state, new_state).generate()

    assert [type(op) for op in ops] == [DeleteModel]


def test_autodetector_no_duplicate_schemas() -> None:
    """Two models in the same schema produce only one CreateSchema."""
    old_state = State()
    new_state = State()

    CreateModel(
        name="Product",
        fields=[("id", fields.IntField(primary_key=True))],
        options={"table": "product", "app": "models", "schema": "custom"},
    ).state_forward("models", new_state)
    CreateModel(
        name="Category",
        fields=[("id", fields.IntField(primary_key=True))],
        options={"table": "category", "app": "models", "schema": "custom"},
    ).state_forward("models", new_state)

    ops = OperationGenerator(old_state, new_state).generate()

    schema_ops = [op for op in ops if isinstance(op, CreateSchema)]
    assert len(schema_ops) == 1
    assert schema_ops[0].schema_name == "custom"


def test_autodetector_no_schema_ops_for_plain_models() -> None:
    """Models without schema produce no schema operations."""
    old_state = State()
    new_state = State()
    _build_state_with_schema_model(new_state, schema=None)

    ops = OperationGenerator(old_state, new_state).generate()

    schema_ops = [op for op in ops if isinstance(op, (CreateSchema, DropSchema))]
    assert len(schema_ops) == 0


def test_autodetector_multiple_schemas_sorted() -> None:
    """Multiple new schemas are created in sorted order."""
    old_state = State()
    new_state = State()

    CreateModel(
        name="Product",
        fields=[("id", fields.IntField(primary_key=True))],
        options={"table": "product", "app": "models", "schema": "warehouse"},
    ).state_forward("models", new_state)
    CreateModel(
        name="Category",
        fields=[("id", fields.IntField(primary_key=True))],
        options={"table": "category", "app": "models", "schema": "catalog"},
    ).state_forward("models", new_state)

    ops = OperationGenerator(old_state, new_state).generate()

    schema_ops = [op for op in ops if isinstance(op, CreateSchema)]
    assert len(schema_ops) == 2
    assert schema_ops[0].schema_name == "catalog"
    assert schema_ops[1].schema_name == "warehouse"


# ---------------------------------------------------------------------------
# 5. MigrationWriter serialization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "operation_class", "expected"),
    [
        pytest.param(
            "0001_initial",
            CreateSchema,
            "ops.CreateSchema(schema_name='custom')",
            id="writer_serializes_create_schema",
        ),
        pytest.param(
            "0002_drop", DropSchema, "ops.DropSchema(schema_name='custom')", id="writer_serializes_drop_schema"
        ),
    ],
)
def test_writer_serializes_a_schema_operation(name, operation_class, expected) -> None:
    writer = MigrationWriter(
        name=name,
        app_label="models",
        operations=[operation_class(schema_name="custom")],
    )
    output = writer.as_string()
    assert expected in output


def test_writer_schema_with_create_model() -> None:
    """Full migration with CreateSchema + CreateModel serializes correctly."""
    writer = MigrationWriter(
        name="0001_initial",
        app_label="models",
        operations=[
            CreateSchema(schema_name="custom"),
            CreateModel(
                name="Product",
                fields=[("id", fields.IntField(primary_key=True))],
                options={"table": "product", "schema": "custom"},
            ),
        ],
    )
    output = writer.as_string()
    assert "ops.CreateSchema(schema_name='custom')" in output
    assert "ops.CreateModel(" in output
    # CreateSchema should come before CreateModel
    schema_pos = output.index("CreateSchema")
    model_pos = output.index("CreateModel")
    assert schema_pos < model_pos


# ---------------------------------------------------------------------------
# 6. through_schema query-time behavior (Issue 3 from code review)
# ---------------------------------------------------------------------------


def _make_m2m_models_with_schema() -> tuple[type[Model], type[Model]]:
    """Build M2M models with Meta.schema for through_schema testing."""

    class STag(Model):
        id = fields.IntField(primary_key=True)
        name = fields.TextField()

        class Meta:
            app = "models"
            table = "tag"
            schema = "catalog"

    class SArticle(Model):
        id = fields.IntField(primary_key=True)
        title = fields.TextField()
        tags: ManyToManyRelation[Any] = fields.ManyToManyField(
            "models.STag", related_name="articles", through="article_tag"
        )

        class Meta:
            app = "models"
            table = "article"
            schema = "catalog"

    init_apps(STag, SArticle)
    return STag, SArticle


def test_through_schema_set_on_forward_m2m_field() -> None:
    """Forward M2M field gets through_schema from declaring model."""
    _, SArticle = _make_m2m_models_with_schema()
    field = cast(ManyToManyFieldInstance, SArticle._meta.fields_map["tags"])
    assert field.through_schema == "catalog"


def test_through_schema_set_on_backward_m2m_field() -> None:
    """Backward (auto-generated) M2M field gets through_schema from declaring model."""
    STag, _ = _make_m2m_models_with_schema()
    field = cast(ManyToManyFieldInstance, STag._meta.fields_map["articles"])
    assert field._generated is True
    assert field.through_schema == "catalog"


def test_through_schema_none_without_schema() -> None:
    """M2M field without Meta.schema has through_schema=None."""

    class PlainA(Model):
        id = fields.IntField(primary_key=True)

        class Meta:
            app = "models"
            table = "plain_a"

    class PlainB(Model):
        id = fields.IntField(primary_key=True)
        a_items: ManyToManyRelation[Any] = fields.ManyToManyField("models.PlainA", related_name="b_items")

        class Meta:
            app = "models"
            table = "plain_b"

    init_apps(PlainA, PlainB)
    field = cast(ManyToManyFieldInstance, PlainB._meta.fields_map["a_items"])
    assert field.through_schema is None


@pytest.mark.parametrize("filter_key", ["tags", "tags__not", "tags__in", "tags__not_in"])
def test_m2m_lookup_joins_the_through_table_in_its_schema(filter_key: str) -> None:
    """A lookup on a many-to-many relation itself joins the through table with its schema."""
    _, SArticle = _make_m2m_models_with_schema()
    value = [1] if filter_key.endswith("in") else 1
    modifier = Q(**{filter_key: value}).get_result(
        ExpressionContext(
            model=SArticle, table=SArticle._meta.basetable, annotations={}, dialect=SQL_DIALECT, connection=None
        )
    )

    through_table: Table = modifier.joins[0][0]
    assert through_table._schema is not None
    assert through_table._schema._name == "catalog"
    assert through_table._table_name == "article_tag"


@pytest.mark.parametrize(
    ("model_index", "table_name", "field_name"),
    [
        pytest.param(1, "article", "tags", id="get_joins_for_m2m_field_includes_schema"),
        # The backward M2M field's through table also gets the schema.
        pytest.param(0, "tag", "articles", id="get_joins_for_backward_m2m_field_includes_schema"),
    ],
)
def test_get_joins_for_an_m2m_field_includes_schema(model_index, table_name, field_name) -> None:
    """LookupPaths.get_joins_for_related_field() creates the through Table with the schema."""
    model_class = _make_m2m_models_with_schema()[model_index]
    field = cast(ManyToManyFieldInstance, model_class._meta.fields_map[field_name])

    base_table = Table(table_name, schema="catalog")
    joins = LookupPaths.get_joins_for_related_field(base_table, field, field_name)

    # First join is to through table
    through_table = joins[0][0]
    assert through_table._table_name == "article_tag"
    assert through_table._schema is not None
    assert through_table._schema._name == "catalog"


@requires_features(supports_schemas=True)
@pytest.mark.asyncio
async def test_rolling_back_create_schema_over_a_schema_holding_a_table_fails_and_keeps_it(db_isolated_no_schema):
    """Rolling back CreateSchema drops the schema without CASCADE - a schema holding a table hare
    doesn't know of is the database's to refuse, never dropped with what it holds."""
    connection = db_isolated_no_schema.get_connection()
    editor = connection.dialect.schema_editor_class(connection, atomic=False)
    state = State()
    old_state = state.clone()
    operation = CreateSchema(schema_name="rollback_kept")
    operation.state_forward("models", state)
    await operation.database_forward("models", old_state, state, editor)
    await connection.execute_script('CREATE TABLE "rollback_kept"."outside" (id INT)')
    try:
        with pytest.raises(DatabaseError):
            await operation.database_backward("models", old_state, state, editor)
        rows = await connection.execute_dicts(
            "SELECT COUNT(*) AS count FROM information_schema.tables "
            "WHERE table_schema = 'rollback_kept' AND table_name = 'outside'"
        )
        assert rows[0]["count"] == 1
    finally:
        await connection.execute_script('DROP SCHEMA IF EXISTS "rollback_kept" CASCADE')


@pytest.mark.asyncio
async def test_moving_a_model_to_another_schema_moves_the_objects_it_declares():
    """AlterModelSchema moves the table and what the model declares in its schema with it: views,
    materialized views, functions, sequences and the functions of its triggers."""
    client = FakeClient("postgresql", inline_comment=False)
    editor = PostgresqlSchemaEditor(client)
    state = State()
    options = {
        "table": "ledger",
        "schema": "old_space",
        "views": [View("ledger_paid", query=RawSQLTerm("SELECT id FROM ledger"))],
        "materialized_views": [
            MaterializedView("ledger_totals", query=RawSQLTerm("SELECT count(*) AS total FROM ledger"))
        ],
        "functions": [
            DatabaseFunction("ledger_count", returns="integer", body=RawSQLTerm("SELECT 1"), arguments=("integer",))
        ],
        "sequences": [DatabaseSequence("ledger_number")],
        "triggers": [Trigger(name="ledger_touch", on=TriggerEvent.UPDATE, body=RawSQLTerm("RETURN NEW;"))],
    }
    await apply_operations(
        "models",
        state,
        CreateModel(name="Ledger", fields=[("id", fields.IntField(primary_key=True))], options=options),
    )

    await apply_operations("models", state, AlterModelSchema(name="Ledger", schema="new_space"), schema_editor=editor)

    assert client.executed == [
        'ALTER TABLE "old_space"."ledger" SET SCHEMA "new_space";',
        'ALTER VIEW "old_space"."ledger_paid" SET SCHEMA "new_space";',
        'ALTER MATERIALIZED VIEW "old_space"."ledger_totals" SET SCHEMA "new_space";',
        'ALTER FUNCTION "old_space"."ledger_count"(integer) SET SCHEMA "new_space";',
        'ALTER SEQUENCE "old_space"."ledger_number" SET SCHEMA "new_space";',
        'ALTER FUNCTION "old_space"."ledger_touch_fn"() SET SCHEMA "new_space";',
    ]
