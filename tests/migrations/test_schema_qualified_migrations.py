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
from hare.ddl.indexes import Index
from hare.dialects.base.constants import SQL_DIALECT
from hare.dialects.base.schema.editor import BaseSchemaEditor
from hare.dialects.postgresql.schema.editor import PostgresqlSchemaEditor
from hare.dialects.sqlite.schema.editor import SqliteSchemaEditor
from hare.fields.relations.fields import ForeignKeyFieldInstance, ManyToManyFieldInstance
from hare.migrations.autodetection.operation_generator import OperationGenerator
from hare.migrations.operations import (
    CreateModel,
    CreateSchema,
    DeleteModel,
    DropSchema,
)
from hare.migrations.state.apps import StateApps
from hare.migrations.state.project import State
from hare.migrations.writer import MigrationWriter
from hare.models import Model
from hare.query.expressions import ExpressionContext, Q
from hare.query.lookup_paths import LookupPaths
from hare.query.queryset.relations.many_to_many_relation import ManyToManyRelation
from hare.sql import Table
from tests.utils.fake_client import FakeClient


def init_apps(*models: type[Model]) -> None:
    apps = StateApps()
    for model in models:
        apps.register_model("models", model)
    apps._init_relations()


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


class _TestEditor(BaseSchemaEditor):
    """Minimal concrete editor for tests (base ANSI SQL dialect)."""

    def _get_table_comment_sql(self, table: str, comment: str) -> str:
        return ""

    def _get_column_comment_sql(self, table: str, column: str, comment: str) -> str:
        return ""


@pytest.mark.asyncio
async def test_base_create_model_with_schema() -> None:
    """Base editor qualifies CREATE TABLE with schema."""
    SchemaCategory, *_ = _make_schema_models()
    client = FakeClient("sql")
    editor = _TestEditor(client)

    await editor.create_model(SchemaCategory)

    sql = client.executed[0]
    assert 'CREATE TABLE "custom"."category"' in sql


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
async def test_base_delete_model_qualified() -> None:
    """DROP TABLE uses schema-qualified name."""
    SchemaCategory, *_ = _make_schema_models()
    client = FakeClient("sql")
    editor = _TestEditor(client)

    await editor.delete_model(SchemaCategory)

    sql = client.executed[0]
    assert 'DROP TABLE "custom"."category"' in sql


@pytest.mark.asyncio
async def test_base_add_field_qualified() -> None:
    """ALTER TABLE ADD COLUMN uses schema-qualified name."""
    SchemaCategory, *_ = _make_schema_models()
    client = FakeClient("sql")
    editor = _TestEditor(client)

    await editor.add_field(SchemaCategory, "name")

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
async def test_sqlite_ignores_schema() -> None:
    """SQLite ignores schema — table name has no schema prefix."""
    SchemaCategory, *_ = _make_schema_models()
    client = FakeClient("sqlite", inline_comment=True)
    editor = SqliteSchemaEditor(client)

    await editor.create_model(SchemaCategory)

    sql = client.executed[0]
    assert 'CREATE TABLE "category"' in sql
    assert '"custom"' not in sql


# ---------------------------------------------------------------------------
# 3. CreateSchema / DropSchema operations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_schema_postgres() -> None:
    client = FakeClient("postgresql", inline_comment=False)
    editor = PostgresqlSchemaEditor(client)

    await editor.create_schema("custom")

    assert client.executed == ['CREATE SCHEMA IF NOT EXISTS "custom";']


@pytest.mark.asyncio
async def test_drop_schema_postgres() -> None:
    client = FakeClient("postgresql", inline_comment=False)
    editor = PostgresqlSchemaEditor(client)

    await editor.drop_schema("custom")

    assert client.executed == ['DROP SCHEMA IF EXISTS "custom" CASCADE;']


@pytest.mark.asyncio
async def test_create_schema_noop_for_base(empty_state: State) -> None:
    """An editor with no schema support (e.g. base ANSI SQL, SQLite) is a silent no-op for
    CreateSchema - create_schema/drop_schema no longer exist on it at all, so the operation's own
    isinstance(state_editor, SchemaAndExtensionSupportMixin) guard is what skips the call."""
    client = FakeClient("sqlite")
    editor = _TestEditor(client)

    op = CreateSchema(schema_name="custom")
    await op.run("models", empty_state, dry_run=False, state_editor=editor)

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
    await create_op.run("models", empty_state, dry_run=False, state_editor=editor)
    drop_op = DropSchema(schema_name="custom")
    await drop_op.run("models", empty_state, dry_run=False, state_editor=editor)

    assert client.executed == []


@pytest.mark.asyncio
async def test_create_schema_operation_runs(empty_state: State) -> None:
    """CreateSchema operation calls schema_editor.create_schema()."""
    client = FakeClient("postgresql", inline_comment=False)
    editor = PostgresqlSchemaEditor(client)

    op = CreateSchema(schema_name="custom")
    await op.run("models", empty_state, dry_run=False, state_editor=editor)

    assert 'CREATE SCHEMA IF NOT EXISTS "custom";' in client.executed


@pytest.mark.asyncio
async def test_drop_schema_operation_runs(empty_state: State) -> None:
    """DropSchema operation calls schema_editor.drop_schema()."""
    client = FakeClient("postgresql", inline_comment=False)
    editor = PostgresqlSchemaEditor(client)

    op = DropSchema(schema_name="custom")
    await op.run("models", empty_state, dry_run=False, state_editor=editor)

    assert 'DROP SCHEMA IF EXISTS "custom" CASCADE;' in client.executed


@pytest.mark.asyncio
async def test_create_schema_operation_backward(empty_state: State) -> None:
    """CreateSchema backward calls drop_schema."""
    client = FakeClient("postgresql", inline_comment=False)
    editor = PostgresqlSchemaEditor(client)

    op = CreateSchema(schema_name="custom")
    old_state = empty_state.clone()
    op.state_forward("models", empty_state)
    await op.database_backward("models", old_state, empty_state, editor)

    assert 'DROP SCHEMA IF EXISTS "custom" CASCADE;' in client.executed


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
    old_state = State(models={}, apps=StateApps())
    new_state = State(models={}, apps=StateApps())
    _build_state_with_schema_model(new_state, schema="custom")

    ops = OperationGenerator(old_state, new_state).generate()

    assert len(ops) >= 2
    assert isinstance(ops[0], CreateSchema)
    assert ops[0].schema_name == "custom"
    assert isinstance(ops[1], CreateModel)


def test_autodetector_generates_drop_schema() -> None:
    """When a schema is no longer used, DropSchema is emitted."""
    old_state = State(models={}, apps=StateApps())
    new_state = State(models={}, apps=StateApps())
    _build_state_with_schema_model(old_state, schema="custom")

    ops = OperationGenerator(old_state, new_state).generate()

    assert len(ops) >= 2
    assert isinstance(ops[0], DeleteModel)
    assert isinstance(ops[1], DropSchema)
    assert ops[1].schema_name == "custom"


def test_autodetector_no_duplicate_schemas() -> None:
    """Two models in the same schema produce only one CreateSchema."""
    old_state = State(models={}, apps=StateApps())
    new_state = State(models={}, apps=StateApps())

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
    old_state = State(models={}, apps=StateApps())
    new_state = State(models={}, apps=StateApps())
    _build_state_with_schema_model(new_state, schema=None)

    ops = OperationGenerator(old_state, new_state).generate()

    schema_ops = [op for op in ops if isinstance(op, (CreateSchema, DropSchema))]
    assert len(schema_ops) == 0


def test_autodetector_multiple_schemas_sorted() -> None:
    """Multiple new schemas are created in sorted order."""
    old_state = State(models={}, apps=StateApps())
    new_state = State(models={}, apps=StateApps())

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


def test_writer_serializes_create_schema() -> None:
    writer = MigrationWriter(
        name="0001_initial",
        app_label="models",
        operations=[CreateSchema(schema_name="custom")],
    )
    output = writer.as_string()
    assert "ops.CreateSchema(schema_name='custom')" in output


def test_writer_serializes_drop_schema() -> None:
    writer = MigrationWriter(
        name="0002_drop",
        app_label="models",
        operations=[DropSchema(schema_name="custom")],
    )
    output = writer.as_string()
    assert "ops.DropSchema(schema_name='custom')" in output


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


def test_get_joins_for_m2m_field_includes_schema() -> None:
    """LookupPaths.get_joins_for_related_field() creates through Table with schema."""
    _, SArticle = _make_m2m_models_with_schema()
    field = cast(ManyToManyFieldInstance, SArticle._meta.fields_map["tags"])

    base_table = Table("article", schema="catalog")
    joins = LookupPaths.get_joins_for_related_field(base_table, field, "tags", dialect=SQL_DIALECT, connection=None)

    # First join is to through table
    through_table = joins[0][0]
    assert through_table._table_name == "article_tag"
    assert through_table._schema is not None
    assert through_table._schema._name == "catalog"


def test_get_joins_for_backward_m2m_field_includes_schema() -> None:
    """Backward M2M field's through table also gets schema."""
    STag, _ = _make_m2m_models_with_schema()
    field = cast(ManyToManyFieldInstance, STag._meta.fields_map["articles"])

    base_table = Table("tag", schema="catalog")
    joins = LookupPaths.get_joins_for_related_field(
        base_table, field, "articles", dialect=SQL_DIALECT, connection=None
    )

    through_table = joins[0][0]
    assert through_table._table_name == "article_tag"
    assert through_table._schema is not None
    assert through_table._schema._name == "catalog"
