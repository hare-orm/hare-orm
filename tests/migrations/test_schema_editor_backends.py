"""Backend-specific schema editor tests: bool defaults, ALTER COLUMN, FK handling, quoting."""

from __future__ import annotations

import pytest

from hare import fields
from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
from hare.dialects.base.schema.tables.table_comments import TableComments
from hare.dialects.dialect_registry import DialectRegistry
from hare.dialects.postgresql.schema.postgresql_schema_editor import PostgresqlSchemaEditor
from hare.exceptions import ConfigurationError
from hare.migrations.state.state_apps import StateApps
from hare.models import Model
from tests.utils.fake_client import FakeClient, MockIntrospectionClient


class TestSchemaEditorTableComments(TableComments):
    def get_table_comment_sql(self, table: str, comment: str) -> str:
        return ""

    def get_column_comment_sql(self, table: str, column: str, comment: str) -> str:
        return ""


class TestSchemaEditor(BaseSchemaEditor):
    table_comments_class = TestSchemaEditorTableComments


def init_apps(*models: type[Model]) -> None:
    apps = StateApps()
    for model in models:
        apps.register_model("models", model)
    apps.init_relations()


# ---------------------------------------------------------------------------
# BooleanField db_default escaping tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_postgres_bool_db_default_true() -> None:
    """PostgreSQL should emit DEFAULT TRUE for BooleanField(db_default=True)."""

    class Widget(Model):
        id = fields.IntField(primary_key=True)
        is_active = fields.BooleanField(db_default=True)

        class Meta:
            table = "widget"
            app = "models"

    client = FakeClient("postgresql", inline_comment=False)
    editor = PostgresqlSchemaEditor(client)
    await editor.add_field(Widget, "is_active")

    assert len(client.executed) == 1
    assert client.executed[0] == 'ALTER TABLE "widget" ADD COLUMN "is_active" BOOL NOT NULL DEFAULT TRUE'


@pytest.mark.asyncio
async def test_postgres_bool_db_default_false() -> None:
    """PostgreSQL should emit DEFAULT FALSE for BooleanField(db_default=False)."""

    class Widget(Model):
        id = fields.IntField(primary_key=True)
        is_active = fields.BooleanField(db_default=False)

        class Meta:
            table = "widget"
            app = "models"

    client = FakeClient("postgresql", inline_comment=False)
    editor = PostgresqlSchemaEditor(client)
    await editor.add_field(Widget, "is_active")

    assert len(client.executed) == 1
    assert client.executed[0] == 'ALTER TABLE "widget" ADD COLUMN "is_active" BOOL NOT NULL DEFAULT FALSE'


@pytest.mark.asyncio
async def test_base_editor_bool_db_default_still_uses_integer() -> None:
    """Non-PostgreSQL backends should still emit DEFAULT 1 for BooleanField(db_default=True)."""

    class Widget(Model):
        id = fields.IntField(primary_key=True)
        is_active = fields.BooleanField(db_default=True)

        class Meta:
            table = "widget"
            app = "models"

    client = FakeClient("sql")
    editor = TestSchemaEditor(client)
    await editor.add_field(Widget, "is_active")

    assert len(client.executed) == 1
    assert client.executed[0] == 'ALTER TABLE "widget" ADD COLUMN "is_active" BOOL NOT NULL DEFAULT 1'


@pytest.mark.asyncio
async def test_postgres_int_db_default_unaffected() -> None:
    """PostgreSQL non-bool db_default should still work (regression guard)."""

    class Widget(Model):
        id = fields.IntField(primary_key=True)
        stock = fields.IntField(db_default=0)

        class Meta:
            table = "widget"
            app = "models"

    client = FakeClient("postgresql", inline_comment=False)
    editor = PostgresqlSchemaEditor(client)
    await editor.add_field(Widget, "stock")

    assert len(client.executed) == 1
    assert client.executed[0] == 'ALTER TABLE "widget" ADD COLUMN "stock" INT NOT NULL DEFAULT 0'


@pytest.mark.asyncio
async def test_postgres_alter_field_bool_db_default() -> None:
    """PostgreSQL alter_field adding db_default=True should emit SET DEFAULT TRUE."""

    class OldWidget(Model):
        id = fields.IntField(primary_key=True)
        is_active = fields.BooleanField(default=True)

        class Meta:
            table = "widget"
            app = "models"

    class NewWidget(Model):
        id = fields.IntField(primary_key=True)
        is_active = fields.BooleanField(default=True, db_default=True)

        class Meta:
            table = "widget"
            app = "models"

    client = FakeClient("postgresql", inline_comment=False)
    editor = PostgresqlSchemaEditor(client)
    await editor.alter_field(OldWidget, NewWidget, "is_active")

    assert len(client.executed) == 1
    assert client.executed[0] == 'ALTER TABLE "widget" ALTER COLUMN "is_active" SET DEFAULT TRUE'


# ---------------------------------------------------------------------------
# RandomHex dialect-aware default tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_random_hex_produces_dialect_specific_sql() -> None:
    """RandomHex should produce different SQL for each dialect."""
    from hare.fields.db_defaults import RandomHex

    rh = RandomHex()
    assert rh.get_sql(DialectRegistry.get_dialect("sqlite")) == "(lower(hex(randomblob(16))))"
    assert rh.get_sql(DialectRegistry.get_dialect("postgresql")) == "md5(random()::text)"


@pytest.mark.asyncio
async def test_now_produces_dialect_specific_sql() -> None:
    """Now should use a higher-precision expression on SQLite, the statement's moment on Postgres."""
    from hare.dialects.sqlite.constants import SQLITE_NOW_UTC_SQL as NOW_SQLITE_UTC_SQL
    from hare.fields.db_defaults import Now

    now = Now()
    assert now.get_sql(DialectRegistry.get_dialect("sqlite")) == NOW_SQLITE_UTC_SQL
    assert now.get_sql(DialectRegistry.get_dialect("postgresql")) == "STATEMENT_TIMESTAMP()"


# ---------------------------------------------------------------------------
# ALTER COLUMN TYPE tests (max_length changes)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_postgres_alter_field_max_length() -> None:
    """PostgreSQL alter_field changing max_length should emit ALTER COLUMN TYPE."""

    class OldWidget(Model):
        id = fields.IntField(primary_key=True)
        name = fields.CharField(max_length=32, null=True)

        class Meta:
            table = "widget"
            app = "models"

    class NewWidget(Model):
        id = fields.IntField(primary_key=True)
        name = fields.CharField(max_length=64, null=True)

        class Meta:
            table = "widget"
            app = "models"

    client = FakeClient("postgresql", inline_comment=False)
    editor = PostgresqlSchemaEditor(client)
    await editor.alter_field(OldWidget, NewWidget, "name")

    assert len(client.executed) == 1
    assert client.executed[0] == 'ALTER TABLE "widget" ALTER COLUMN "name" TYPE VARCHAR(64) USING "name"::VARCHAR(64)'


# ---------------------------------------------------------------------------
# alter_field() field-CLASS change tests (used to hard-block citing a nonexistent
# AlterFieldManual escape hatch, before ever reaching dialect-specific handling)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_postgres_alter_field_class_change_emits_alter_column_type() -> None:
    """IntField -> DecimalField (a genuine Python-type change, previously hard-blocked with
    "Please use AlterFieldManual") must now reach Postgres's real ALTER COLUMN TYPE path."""

    class OldWidget(Model):
        id = fields.IntField(primary_key=True)
        amount = fields.IntField()

        class Meta:
            table = "widget"
            app = "models"

    class NewWidget(Model):
        id = fields.IntField(primary_key=True)
        amount = fields.DecimalField(max_digits=12, decimal_places=2)

        class Meta:
            table = "widget"
            app = "models"

    client = FakeClient("postgresql", inline_comment=False)
    editor = PostgresqlSchemaEditor(client)
    await editor.alter_field(OldWidget, NewWidget, "amount")

    assert len(client.executed) == 1
    assert (
        client.executed[0]
        == 'ALTER TABLE "widget" ALTER COLUMN "amount" TYPE DECIMAL(12,2) USING "amount"::DECIMAL(12,2)'
    )


@pytest.mark.asyncio
async def test_alter_field_plain_to_relation_raises() -> None:
    """Converting a plain column into a relation field (FK/O2O/M2M) via AlterField isn't a
    column-level ALTER at all - the relation field is backed by shadow columns/a through table,
    not a single column of its own - so this must still raise, with a message that doesn't cite
    a nonexistent escape hatch."""

    class Parent(Model):
        id = fields.IntField(primary_key=True)

        class Meta:
            table = "parent"
            app = "models"

    class OldWidget(Model):
        id = fields.IntField(primary_key=True)
        parent = fields.IntField()

        class Meta:
            table = "widget"
            app = "models"

    class NewWidget(Model):
        id = fields.IntField(primary_key=True)
        parent = fields.ForeignKeyField("models.Parent")

        class Meta:
            table = "widget"
            app = "models"

    init_apps(Parent, NewWidget)

    client = FakeClient("postgresql", inline_comment=False)
    editor = PostgresqlSchemaEditor(client)
    with pytest.raises(ConfigurationError, match="relation field"):
        await editor.alter_field(OldWidget, NewWidget, "parent")


@pytest.mark.asyncio
async def test_alter_field_relation_to_plain_raises() -> None:
    """Sibling of the test above for the OTHER direction (relation -> plain column)."""

    class Parent(Model):
        id = fields.IntField(primary_key=True)

        class Meta:
            table = "parent"
            app = "models"

    class OldWidget(Model):
        id = fields.IntField(primary_key=True)
        parent = fields.ForeignKeyField("models.Parent")

        class Meta:
            table = "widget"
            app = "models"

    class NewWidget(Model):
        id = fields.IntField(primary_key=True)
        parent = fields.IntField()

        class Meta:
            table = "widget"
            app = "models"

    init_apps(Parent, OldWidget)

    client = FakeClient("postgresql", inline_comment=False)
    editor = PostgresqlSchemaEditor(client)
    with pytest.raises(ConfigurationError, match="relation field"):
        await editor.alter_field(OldWidget, NewWidget, "parent")


# ---------------------------------------------------------------------------
# Bug 2: Description changes emit real SQL (Issue #2141)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_postgres_alter_field_description_change() -> None:
    """PostgreSQL should emit COMMENT ON COLUMN when description changes."""

    class OldWidget(Model):
        id = fields.IntField(primary_key=True)
        name = fields.CharField(max_length=100, description="item name")

        class Meta:
            table = "item"
            app = "models"

    class NewWidget(Model):
        id = fields.IntField(primary_key=True)
        name = fields.CharField(max_length=100, description="short item name")

        class Meta:
            table = "item"
            app = "models"

    client = FakeClient("postgresql", inline_comment=False)
    editor = PostgresqlSchemaEditor(client)
    await editor.alter_field(OldWidget, NewWidget, "name")

    assert len(client.executed) == 1
    assert "COMMENT ON COLUMN" in client.executed[0]
    assert "short item name" in client.executed[0]


@pytest.mark.asyncio
async def test_postgres_alter_field_description_removal() -> None:
    """PostgreSQL should emit COMMENT ON COLUMN ... IS NULL when description removed."""

    class OldWidget(Model):
        id = fields.IntField(primary_key=True)
        name = fields.CharField(max_length=100, description="item name")

        class Meta:
            table = "item"
            app = "models"

    class NewWidget(Model):
        id = fields.IntField(primary_key=True)
        name = fields.CharField(max_length=100)

        class Meta:
            table = "item"
            app = "models"

    client = FakeClient("postgresql", inline_comment=False)
    editor = PostgresqlSchemaEditor(client)
    await editor.alter_field(OldWidget, NewWidget, "name")

    assert len(client.executed) == 1
    assert "IS NULL" in client.executed[0]


@pytest.mark.asyncio
async def test_base_alter_field_description_change_noop() -> None:
    """Base editor should emit no SQL for description-only changes (unsupported)."""

    class OldWidget(Model):
        id = fields.IntField(primary_key=True)
        name = fields.CharField(max_length=100, description="old desc")

        class Meta:
            table = "item"
            app = "models"

    class NewWidget(Model):
        id = fields.IntField(primary_key=True)
        name = fields.CharField(max_length=100, description="new desc")

        class Meta:
            table = "item"
            app = "models"

    client = FakeClient("sql")
    editor = TestSchemaEditor(client)
    await editor.alter_field(OldWidget, NewWidget, "name")

    assert len(client.executed) == 0


# ---------------------------------------------------------------------------
# db_constraint=False FK/M2M handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_postgres_add_field_foreign_key_db_constraint_false_omits_reference() -> None:
    """db_constraint=False must produce a plain column with no FK constraint at all, matching
    what BaseSchemaGenerator.generate_schemas() already does for the same field."""

    class Category(Model):
        id = fields.IntField(primary_key=True)

        class Meta:
            table = "category"
            app = "models"

    class Item(Model):
        id = fields.IntField(primary_key=True)
        category: fields.ForeignKeyRelation[Category] = fields.ForeignKeyField("models.Category", db_constraint=False)

        class Meta:
            table = "item"
            app = "models"

    init_apps(Category, Item)

    client = FakeClient("postgresql", inline_comment=False)
    editor = PostgresqlSchemaEditor(client)
    await editor.add_field(Item, "category")

    assert len(client.executed) == 1
    sql = client.executed[0]
    assert 'ALTER TABLE "item" ADD COLUMN' in sql
    assert '"category_id"' in sql
    assert "REFERENCES" not in sql


@pytest.mark.asyncio
async def test_postgres_add_field_m2m_db_constraint_false_omits_both_references() -> None:
    """Same db_constraint=False guarantee for a ManyToManyField's through-table."""

    class Tag(Model):
        id = fields.IntField(primary_key=True)

        class Meta:
            table = "tag"
            app = "models"

    class Post(Model):
        id = fields.IntField(primary_key=True)
        tags: fields.ManyToManyRelation[Tag] = fields.ManyToManyField(
            "models.Tag", related_name="posts", db_constraint=False
        )

        class Meta:
            table = "post"
            app = "models"

    init_apps(Tag, Post)

    client = FakeClient("postgresql", inline_comment=False)
    editor = PostgresqlSchemaEditor(client)
    await editor.add_field(Post, "tags")

    assert len(client.executed) == 1
    sql = client.executed[0]
    assert "REFERENCES" not in sql
    assert "ON DELETE" not in sql


@pytest.mark.asyncio
async def test_postgres_alter_field_db_constraint_true_to_false_drops_constraint() -> None:
    """A pure db_constraint=True -> False change (on_delete unchanged) used to be a complete
    no-op at the DB level - alter_field() only ever compared on_delete, so the physical FOREIGN
    KEY constraint was left in place even though the model no longer declares one. Mirrors
    test_composite_target_fk_migration_lifecycle's own on_delete-change pattern (a copy() of the
    SAME registered field, mutated and passed straight to _alter_fk_on_delete()) rather than two
    separately-declared model classes, which would trigger an unrelated column rename/retype."""
    from copy import copy as copy_field

    class Category(Model):
        id = fields.IntField(primary_key=True)

        class Meta:
            table = "category"
            app = "models"

    class Item(Model):
        id = fields.IntField(primary_key=True)
        category: fields.ForeignKeyRelation[Category] = fields.ForeignKeyField("models.Category")

        class Meta:
            table = "item"
            app = "models"

    init_apps(Category, Item)

    old_field = Item._meta.fields_map["category"]
    new_field = copy_field(old_field)
    new_field.db_constraint = False

    client = MockIntrospectionClient(
        "postgresql", constraint_names=[{"conname": "item_category_id_fkey"}], inline_comment=False
    )
    editor = PostgresqlSchemaEditor(client)
    await editor.foreign_key_rebuild.alter_foreign_key_on_delete(
        Item, old_field.source_fields[0], old_field, new_field
    )

    assert len(client.executed) == 1
    assert client.executed[0] == 'ALTER TABLE "item" DROP CONSTRAINT "item_category_id_fkey"'


@pytest.mark.asyncio
async def test_postgres_alter_field_db_constraint_false_to_true_adds_constraint() -> None:
    """Sibling of the test above for the other direction - no constraint exists yet (old field
    had db_constraint=False), and one must be added once the new field turns it on."""
    from copy import copy as copy_field

    class Category(Model):
        id = fields.IntField(primary_key=True)

        class Meta:
            table = "category"
            app = "models"

    class Item(Model):
        id = fields.IntField(primary_key=True)
        category: fields.ForeignKeyRelation[Category] = fields.ForeignKeyField("models.Category", db_constraint=False)

        class Meta:
            table = "item"
            app = "models"

    init_apps(Category, Item)

    old_field = Item._meta.fields_map["category"]
    new_field = copy_field(old_field)
    new_field.db_constraint = True

    client = MockIntrospectionClient("postgresql", constraint_names=[], inline_comment=False)
    editor = PostgresqlSchemaEditor(client)
    await editor.foreign_key_rebuild.alter_foreign_key_on_delete(
        Item, old_field.source_fields[0], old_field, new_field
    )

    assert len(client.executed) == 1
    sql = client.executed[0]
    assert 'ALTER TABLE "item" ADD CONSTRAINT' in sql
    assert 'FOREIGN KEY ("category_id") REFERENCES "category" ("id")' in sql
