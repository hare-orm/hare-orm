from __future__ import annotations

from typing import cast

import pytest

from hare import fields
from hare.ddl import RawSQLTerm
from hare.ddl.generated_names import GeneratedNames
from hare.ddl.indexes import Index, PartialIndex
from hare.dialects.base.schema.editor import BaseSchemaEditor
from hare.dialects.postgresql.fields.search import TSVectorField
from hare.dialects.postgresql.fields.vector import VectorField
from hare.dialects.postgresql.indexes import BloomIndex, BrinIndex, GinIndex, GistIndex, IvfflatIndex, SpGistIndex
from hare.dialects.postgresql.schema.editor import PostgresqlSchemaEditor
from hare.exceptions import ConfigurationError
from hare.migrations.state.apps import StateApps
from hare.models import Model
from tests.utils.fake_client import FakeClient


class TestSchemaEditor(BaseSchemaEditor):
    def _get_table_comment_sql(self, table: str, comment: str) -> str:
        return ""

    def _get_column_comment_sql(self, table: str, column: str, comment: str) -> str:
        return ""


class Widget(Model):
    id = fields.IntField(primary_key=True)
    name = fields.TextField()

    class Meta:
        table = "widget"
        app = "models"


def init_apps(*models: type[Model]) -> None:
    apps = StateApps()
    for model in models:
        apps.register_model("models", model)
    apps._init_relations()


@pytest.mark.asyncio
async def test_create_model_generates_table_sql() -> None:
    client = FakeClient("sql")
    editor = TestSchemaEditor(client)

    await editor.create_model(Widget)

    assert len(client.executed) == 1
    sql = client.executed[0]
    assert 'CREATE TABLE "widget"' in sql
    assert '"id" INT' in sql
    assert "PRIMARY KEY" in sql


@pytest.mark.asyncio
async def test_add_field_generates_add_column_sql() -> None:
    client = FakeClient("sql")
    editor = TestSchemaEditor(client)

    await editor.add_field(Widget, "name")

    assert len(client.executed) == 1
    sql = client.executed[0]
    assert 'ALTER TABLE "widget" ADD COLUMN' in sql
    assert '"name" TEXT' in sql


@pytest.mark.asyncio
async def test_remove_field_generates_drop_column_sql() -> None:
    client = FakeClient("sql")
    editor = TestSchemaEditor(client)

    await editor.remove_field(Widget, Widget._meta.fields_map["name"])

    assert len(client.executed) == 1
    assert client.executed[0] == 'ALTER TABLE "widget" DROP COLUMN "name" CASCADE'


@pytest.mark.asyncio
async def test_add_field_m2m_generates_table_sql() -> None:
    class Tag(Model):
        id = fields.IntField(primary_key=True)
        name = fields.TextField()

        class Meta:
            table = "tag"
            app = "models"

    class WidgetWithTags(Model):
        id = fields.IntField(primary_key=True)
        tags = fields.ManyToManyField(Tag, related_name="widgets")

        class Meta:
            table = "widget"
            app = "models"

    init_apps(Tag, WidgetWithTags)

    client = FakeClient("sql")
    editor = TestSchemaEditor(client)

    await editor.add_field(WidgetWithTags, "tags")

    assert client.executed
    assert 'CREATE TABLE "widget_tag"' in client.executed[0]


@pytest.mark.asyncio
async def test_create_model_generates_generated_column_sql() -> None:
    class Document(Model):
        id = fields.IntField(primary_key=True)
        title = fields.TextField()
        body = fields.TextField(null=True)
        search_vector = TSVectorField(
            source_fields=("title", "body"),
            config="english",
            weights=("A", "B"),
        )

        class Meta:
            table = "document"
            app = "models"

    client = FakeClient("postgresql", inline_comment=False)
    editor = PostgresqlSchemaEditor(client)

    await editor.create_model(Document)

    assert client.executed
    sql = client.executed[0]
    assert 'CREATE TABLE "document"' in sql
    assert (
        "\"search_vector\" TSVECTOR GENERATED ALWAYS AS (SETWEIGHT(TO_TSVECTOR('english',"
        "COALESCE(\"title\", '')),'A') || SETWEIGHT(TO_TSVECTOR('english',"
        "COALESCE(\"body\", '')),'B')) STORED"
    ) in sql


@pytest.mark.asyncio
async def test_add_field_generates_generated_column_sql() -> None:
    class Document(Model):
        id = fields.IntField(primary_key=True)
        title = fields.TextField()
        body = fields.TextField(null=True)
        search_vector = TSVectorField(
            source_fields=("title", "body"),
            config="english",
            weights=("A", "B"),
        )

        class Meta:
            table = "document"
            app = "models"

    client = FakeClient("postgresql", inline_comment=False)
    editor = PostgresqlSchemaEditor(client)

    await editor.add_field(Document, "search_vector")

    assert client.executed
    sql = client.executed[0]
    assert 'ALTER TABLE "document" ADD COLUMN' in sql
    assert (
        "\"search_vector\" TSVECTOR GENERATED ALWAYS AS (SETWEIGHT(TO_TSVECTOR('english',"
        "COALESCE(\"title\", '')),'A') || SETWEIGHT(TO_TSVECTOR('english',"
        "COALESCE(\"body\", '')),'B')) STORED"
    ) in sql


@pytest.mark.asyncio
async def test_add_index_generates_gin_tsvector_sql() -> None:
    class Document(Model):
        id = fields.IntField(primary_key=True)
        search_vector = TSVectorField()

        class Meta:
            table = "document"
            app = "models"
            indexes = [GinIndex(fields=("search_vector",))]

    client = FakeClient("postgresql", inline_comment=False)
    editor = PostgresqlSchemaEditor(client)

    index = cast(Index, Document._meta.indexes[0])
    await editor.add_index(Document, index)

    assert client.executed
    expected_name = GeneratedNames.get_index_name("idx", Document, ["search_vector"], ("GIN",))
    assert (f'CREATE INDEX "{expected_name}" ON "document" USING GIN ("search_vector");') == client.executed[0]


@pytest.mark.asyncio
async def test_add_index_renders_with_storage_params_before_where_condition() -> None:
    """Postgres syntax requires storage parameters (WITH (...)) to come BEFORE a partial index's
    condition (WHERE (...)) - PartialIndex.__init__ only ever APPENDS its WHERE clause onto
    self.extra, so IvfflatIndex/HnswIndex must PREPEND their own WITH (...) fragment onto
    whatever self.extra ends up holding, not append after it. Combining lists= with condition=
    together is the regression case: getting the ordering backwards would generate syntactically
    invalid DDL (WHERE before WITH)."""

    class Item(Model):
        id = fields.IntField(primary_key=True)
        active = fields.BooleanField(default=True)
        embedding = VectorField(dimensions=3)

        class Meta:
            table = "item"
            app = "models"
            indexes = [IvfflatIndex(fields=("embedding",), lists=100, condition=RawSQLTerm("active"))]

    client = FakeClient("postgresql", inline_comment=False)
    editor = PostgresqlSchemaEditor(client)

    index = cast(Index, Item._meta.indexes[0])
    await editor.add_index(Item, index)

    assert client.executed
    sql = client.executed[0]
    with_pos = sql.index("WITH (lists=100)")
    where_pos = sql.index("WHERE")
    assert with_pos < where_pos, f"WITH must come before WHERE: {sql!r}"
    expected_name = GeneratedNames.get_index_name("idx", Item, ["embedding"], index.get_name_parts())
    assert sql == (
        f'CREATE INDEX "{expected_name}" ON "item" USING IVFFLAT ("embedding") WITH (lists=100) WHERE (active);'
    )


def test_generate_index_name_resists_collisions_across_similarly_named_tables() -> None:
    """A 6-hex-char (24-bit) digest suffix let two DIFFERENT (table, field) combinations that
    happen to share the same truncated table/field prefix collide into the IDENTICAL generated
    index name after only a few thousand candidates (birthday bound for a 24-bit space) - a
    realistic risk for a schema with many similarly-named tables (e.g. multi-tenant
    "tenant_1_orders"/"tenant_2_orders"). Every candidate below shares the same 11-char table
    prefix ("same_prefix") and 7-char field prefix ("same_fi"), isolating the digest itself as
    the only thing that can possibly disambiguate them."""
    seen: dict[str, tuple[str, str]] = {}
    for i in range(20_000):
        table_name = f"same_prefix_table_{i}"
        field_name = f"same_field_{i}"
        name = GeneratedNames.get_index_name("idx", table_name, [field_name])
        collision = seen.get(name)
        assert collision is None, f"{name!r} generated for both {collision!r} and {(table_name, field_name)!r}"
        seen[name] = (table_name, field_name)


@pytest.mark.asyncio
async def test_alter_generated_field_raises() -> None:
    class OldDocument(Model):
        id = fields.IntField(primary_key=True)
        title = fields.TextField()
        body = fields.TextField(null=True)
        search_vector = TSVectorField(
            source_fields=("title",),
            config="english",
        )

        class Meta:
            table = "document"
            app = "models"

    class NewDocument(Model):
        id = fields.IntField(primary_key=True)
        title = fields.TextField()
        body = fields.TextField(null=True)
        search_vector = TSVectorField(
            source_fields=("title", "body"),
            config="english",
        )

        class Meta:
            table = "document"
            app = "models"

    client = FakeClient("postgresql", inline_comment=False)
    editor = PostgresqlSchemaEditor(client)

    with pytest.raises(ConfigurationError):
        await editor.alter_field(OldDocument, NewDocument, "search_vector")
    assert not client.executed


@pytest.mark.asyncio
async def test_create_model_includes_db_default() -> None:
    """CreateModel should include DEFAULT clause for fields with db_default."""

    class WidgetWithDefault(Model):
        id = fields.IntField(primary_key=True)
        status = fields.CharField(max_length=20, db_default="active")

        class Meta:
            table = "widget"
            app = "models"

    client = FakeClient("sql")
    editor = TestSchemaEditor(client)

    await editor.create_model(WidgetWithDefault)

    assert len(client.executed) == 1
    sql = client.executed[0]
    assert 'CREATE TABLE "widget"' in sql
    assert "DEFAULT 'active'" in sql


@pytest.mark.asyncio
async def test_create_model_includes_db_default_on_fk() -> None:
    """CreateModel should include DEFAULT clause for FK columns with db_default."""

    class Dc(Model):
        id = fields.IntField(primary_key=True)

        class Meta:
            table = "dc"
            app = "models"

    class App(Model):
        id = fields.IntField(primary_key=True)
        dc: fields.ForeignKeyRelation[Dc] = fields.ForeignKeyField("models.Dc", db_default=2)

        class Meta:
            table = "app"
            app = "models"

    init_apps(Dc, App)

    client = FakeClient("sql")
    editor = TestSchemaEditor(client)

    await editor.create_model(App)

    assert len(client.executed) == 1
    sql = client.executed[0]
    assert 'CREATE TABLE "app"' in sql
    assert '"dc_id"' in sql
    assert "DEFAULT 2" in sql


def test_generated_index_name_of_plain_index_depends_on_table_and_columns_only() -> None:
    """A plain btree index keeps the name it always had - no access method, operator class,
    storage parameter or condition to tell it apart from."""
    assert Index(fields=("title",)).get_name_parts() == ()
    assert GeneratedNames.get_index_name("idx", "item", ["title"], ()) == GeneratedNames.get_index_name(
        "idx", "item", ["title"]
    )


@pytest.mark.parametrize(
    ("first_index", "second_index"),
    [
        (GistIndex(fields=("span",)), SpGistIndex(fields=("span",))),
        (BrinIndex(fields=("span",)), BloomIndex(fields=("span",))),
        (Index(fields=("span",)), PartialIndex(fields=("span",), condition=RawSQLTerm("active"))),
        (Index(fields=("span",)), Index(fields=("span",), opclasses=("varchar_pattern_ops",))),
        (IvfflatIndex(fields=("span",), lists=1), IvfflatIndex(fields=("span",), lists=2)),
    ],
)
def test_generated_index_names_differ_for_indexes_on_the_same_columns(first_index, second_index) -> None:
    """Two unnamed indexes on the same columns differing only in access method, condition,
    operator class or storage parameters used to get one name - "relation already exists"."""
    first_name = GeneratedNames.get_index_name("idx", "item", ["span"], first_index.get_name_parts())
    second_name = GeneratedNames.get_index_name("idx", "item", ["span"], second_index.get_name_parts())

    assert first_name != second_name
