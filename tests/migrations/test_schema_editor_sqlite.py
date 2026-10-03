from __future__ import annotations

import pytest

from hare import fields
from hare.ddl import RawSQLTerm
from hare.ddl.constraints import UniqueConstraint
from hare.dialects.sqlite.client import SqliteClient
from hare.dialects.sqlite.schema.editor import SqliteSchemaEditor
from hare.migrations.state.apps import StateApps
from hare.models import Model
from tests.utils.fake_client import FakeClient


class Widget(Model):
    id = fields.IntField(primary_key=True)
    slug = fields.CharField(max_length=50, unique=True)

    class Meta:
        table = "widget"
        app = "models"


def init_apps(*models: type[Model]) -> None:
    apps = StateApps()
    for model in models:
        apps.register_model("models", model)
    apps._init_relations()


@pytest.mark.asyncio
async def test_sqlite_add_field_unique_uses_index() -> None:
    client = FakeClient("sqlite")
    editor = SqliteSchemaEditor(client)

    await editor.add_field(Widget, "slug")

    assert len(client.executed) == 2
    add_column_sql = client.executed[0]
    unique_index_sql = client.executed[1]
    assert 'ALTER TABLE "widget" ADD COLUMN' in add_column_sql
    assert "UNIQUE" not in add_column_sql
    assert "CREATE UNIQUE INDEX" in unique_index_sql
    assert '"slug"' in unique_index_sql


@pytest.mark.asyncio
async def test_sqlite_add_field_foreign_key_includes_inline_reference() -> None:
    """add_field()'s ForeignKeyFieldInstance branch - previously untested - builds the ADD
    COLUMN definition together with its inline REFERENCES clause, the same shape create_model()
    would emit for the same column if it existed from the start."""

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

    client = FakeClient("sqlite")
    editor = SqliteSchemaEditor(client)

    await editor.add_field(Item, "category")

    assert len(client.executed) == 1
    sql = client.executed[0]
    assert 'ALTER TABLE "item" ADD COLUMN' in sql
    assert '"category_id"' in sql
    assert 'REFERENCES "category" ("id")' in sql


@pytest.mark.asyncio
async def test_sqlite_add_field_foreign_key_db_constraint_false_omits_reference() -> None:
    """db_constraint=False must produce a plain column with no FK constraint at all, matching
    what BaseSchemaGenerator.generate_schemas() already does for the same field - add_field()
    previously always appended the REFERENCES clause regardless of db_constraint."""

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

    client = FakeClient("sqlite")
    editor = SqliteSchemaEditor(client)

    await editor.add_field(Item, "category")

    assert len(client.executed) == 1
    sql = client.executed[0]
    assert 'ALTER TABLE "item" ADD COLUMN' in sql
    assert '"category_id"' in sql
    assert "REFERENCES" not in sql


@pytest.mark.asyncio
async def test_sqlite_add_field_foreign_key_with_description_includes_comment() -> None:
    """Same FK branch, but with a field description set - exercises the comment-building
    sub-branch (previously also untested) that only runs when fk_field.description is set."""

    class Category(Model):
        id = fields.IntField(primary_key=True)

        class Meta:
            table = "category"
            app = "models"

    class Item(Model):
        id = fields.IntField(primary_key=True)
        category: fields.ForeignKeyRelation[Category] = fields.ForeignKeyField(
            "models.Category", description="the item's category"
        )

        class Meta:
            table = "item"
            app = "models"

    init_apps(Category, Item)

    client = FakeClient("sqlite")
    editor = SqliteSchemaEditor(client)

    await editor.add_field(Item, "category")

    assert len(client.executed) == 1
    assert "the item's category" in client.executed[0]


@pytest.mark.asyncio
async def test_sqlite_add_field_m2m_db_constraint_false_omits_both_references() -> None:
    """db_constraint=False on a ManyToManyField must produce a through-table with no FK
    constraint on either column at all, matching what BaseSchemaGenerator.generate_schemas()
    already does for the same field - the M2M through-table builder previously always hardcoded
    both FK reference clauses (and always ON DELETE CASCADE, ignoring the field's own on_delete
    too) regardless of db_constraint."""

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

    client = FakeClient("sqlite")
    editor = SqliteSchemaEditor(client)

    await editor.add_field(Post, "tags")

    assert len(client.executed) == 1
    sql = client.executed[0]
    assert "REFERENCES" not in sql
    assert "ON DELETE" not in sql


@pytest.mark.asyncio
async def test_sqlite_add_field_m2m_honors_on_delete() -> None:
    """The through-table's FK columns must use the field's own on_delete, not a hardcoded
    ON DELETE CASCADE regardless of what was actually configured."""
    from hare.fields.enums import OnDelete

    class Tag(Model):
        id = fields.IntField(primary_key=True)

        class Meta:
            table = "tag"
            app = "models"

    class Post(Model):
        id = fields.IntField(primary_key=True)
        tags: fields.ManyToManyRelation[Tag] = fields.ManyToManyField(
            "models.Tag", related_name="posts", on_delete=OnDelete.RESTRICT
        )

        class Meta:
            table = "post"
            app = "models"

    init_apps(Tag, Post)

    client = FakeClient("sqlite")
    editor = SqliteSchemaEditor(client)

    await editor.add_field(Post, "tags")

    assert len(client.executed) == 1
    sql = client.executed[0]
    assert "ON DELETE CASCADE" not in sql


@pytest.mark.asyncio
async def test_sqlite_add_field_m2m_set_null_produces_nullable_columns() -> None:
    """on_delete=SET_NULL needs the through-table's own FK column(s) to accept NULL for the
    database's ON DELETE SET NULL constraint action to be legal DDL - mirrors
    BaseSchemaGenerator's own nullable-column handling for the same field, via this migration
    add_field() path instead of generate_schemas()."""
    from hare.fields.enums import OnDelete

    class Tag(Model):
        id = fields.IntField(primary_key=True)

        class Meta:
            table = "tag"
            app = "models"

    class Post(Model):
        id = fields.IntField(primary_key=True)
        tags: fields.ManyToManyRelation[Tag] = fields.ManyToManyField(
            "models.Tag", related_name="posts", on_delete=OnDelete.SET_NULL
        )

        class Meta:
            table = "post"
            app = "models"

    init_apps(Tag, Post)

    client = FakeClient("sqlite")
    editor = SqliteSchemaEditor(client)

    await editor.add_field(Post, "tags")

    assert len(client.executed) == 1
    sql = client.executed[0]
    assert "ON DELETE SET NULL" in sql
    assert '"tag_id" INT NOT NULL' not in sql
    assert '"post_id" INT NOT NULL' not in sql


@pytest.mark.asyncio
async def test_sqlite_add_constraint_with_condition_creates_partial_unique_index() -> None:
    """SQLite supports partial indexes - a conditional UniqueConstraint becomes a unique index
    with a WHERE clause, as on Postgres."""
    client = FakeClient("sqlite")
    editor = SqliteSchemaEditor(client)

    constraint = UniqueConstraint(fields=("slug",), name="uq_active_slug", condition=RawSQLTerm("is_active = 1"))
    await editor.add_constraint(Widget, constraint)
    assert client.executed == ['CREATE UNIQUE INDEX "uq_active_slug" ON "widget" ("slug") WHERE is_active = 1;']


@pytest.mark.asyncio
async def test_sqlite_remove_constraint_with_condition_drops_its_index() -> None:
    """A conditional UniqueConstraint is removed by dropping its own index by name."""
    client = FakeClient("sqlite")
    editor = SqliteSchemaEditor(client)

    constraint = UniqueConstraint(fields=("slug",), name="uq_active_slug", condition=RawSQLTerm("is_active = 1"))
    await editor.remove_constraint(Widget, constraint)
    assert client.executed == ['DROP INDEX "uq_active_slug"']


@pytest.mark.asyncio
async def test_sqlite_rename_constraint_preserves_uniqueness() -> None:
    """Regression: SQLite has no ALTER TABLE ... RENAME CONSTRAINT, so rename_constraint() must
    drop the old unique index and rebuild a NEW one - a previous version of this method routed
    through the generic (constraint-unaware) rename_index() helper instead, which silently
    rebuilt the renamed index as a plain (non-unique) one, dropping the uniqueness guarantee
    every time a UniqueConstraint was renamed on SQLite."""
    client = FakeClient("sqlite")
    editor = SqliteSchemaEditor(client)

    old_constraint = UniqueConstraint(fields=("slug",), name="uq_widget_slug")
    new_constraint = UniqueConstraint(fields=("slug",), name="uq_widget_slug_renamed")

    await editor.rename_constraint(Widget, old_constraint, new_constraint)

    assert len(client.executed) == 2
    assert client.executed[0] == 'DROP INDEX "uq_widget_slug"'
    assert "CREATE UNIQUE INDEX" in client.executed[1]
    assert "uq_widget_slug_renamed" in client.executed[1]


@pytest.mark.asyncio
async def test_sqlite_alter_field_rename_and_type_change_applies_both() -> None:
    """The RENAME-COLUMN fast path is only valid for a pure rename - if the SQL type also
    changed, it must fall through to the full table-recreation path so the type change isn't
    silently dropped."""
    client = FakeClient("sqlite")
    editor = SqliteSchemaEditor(client)

    old_field = fields.CharField(max_length=50)
    old_field.model_field_name = "slug"
    new_field = fields.CharField(max_length=100, source_field="new_slug")
    new_field.model_field_name = "slug"

    await editor._alter_field(Widget, old_field, new_field)

    # Not the single-statement RENAME COLUMN fast path - the type change forces a rebuild.
    assert not (len(client.executed) == 1 and "RENAME COLUMN" in client.executed[0])
    create_new_table_sql = next(sql for sql in client.executed if sql.startswith('CREATE TABLE "new__widget"'))
    assert "VARCHAR(100)" in create_new_table_sql
    assert '"new_slug"' in create_new_table_sql


@pytest.mark.asyncio
async def test_sqlite_constraint_name_used_for_drop() -> None:
    client = FakeClient("sqlite")
    editor = SqliteSchemaEditor(client)

    constraint = UniqueConstraint(fields=("slug",))
    constraint_name = editor._constraint_name_for_model(Widget, constraint)

    await editor.add_constraint(Widget, constraint)
    await editor.remove_constraint(Widget, constraint)

    assert constraint_name in client.executed[0]
    assert f'DROP INDEX "{constraint_name}"' in client.executed[1]


def test_sqlite_split_statements_ignores_semicolons_in_literals_comments_and_triggers() -> None:
    script = (
        'CREATE TABLE "t" ("a" TEXT DEFAULT \'x;y\', "b" TEXT /* c; d */, "e;f" INT); '
        "-- trailing; comment\n"
        'CREATE TRIGGER tr AFTER INSERT ON "t" BEGIN\nUPDATE "t" SET a = \'q\';\nDELETE FROM "t";\nEND;\n'
        'CREATE INDEX i ON "t" ("a")'
    )

    statements = SqliteClient.split_script(script)

    assert len(statements) == 3
    assert statements[0].startswith("CREATE TABLE") and statements[0].endswith("INT)")
    assert statements[1].startswith("-- trailing; comment\nCREATE TRIGGER") and statements[1].endswith("END")
    assert statements[2] == 'CREATE INDEX i ON "t" ("a")'
