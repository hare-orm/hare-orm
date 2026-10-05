"""Regression tests for the DDL identifier-quoting gap in schema generation: a field's
``source_field``, a ManyToManyField's ``forward_key``/``backward_key``, and a
Meta.constraints entry's ``name`` all used to be spliced straight into a literal
``"{name}"``-quoted template with no escaping - none of them are validated against SQL
metacharacters anywhere upstream, so an embedded ``"`` broke out of the quoted identifier
into raw DDL. The same class of bug was already fixed for ``PartialIndex.condition`` (see
tests/test_indexes.py); this covers the remaining splice points via ``self.quote()``, the
same mechanism that fix used.

Every case asserts the adversarial identifier round-trips through self.quote()'s "double an
embedded quote" convention (an even number of quote characters in the generated SQL, and the
expected doubled-quote form present verbatim) - not just "no exception was raised". The sqlite
cases additionally execute the generated DDL against a real sqlite3 connection and round-trip
a value through the adversarially-named column, proving the escaping is also functionally
correct, not just textually balanced.
"""

from __future__ import annotations

import sqlite3

import pytest

from hare import fields
from hare.ddl import RawSQLTerm
from hare.ddl.constraints import CheckConstraint, ExclusionConstraint, UniqueConstraint
from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
from hare.dialects.base.schema.tables.table_comments import TableComments
from hare.dialects.postgresql.schema.postgresql_schema_editor import PostgresqlSchemaEditor
from hare.dialects.sqlite.schema.sqlite_schema_editor import SqliteSchemaEditor
from hare.migrations.state.state_apps import StateApps
from hare.models import Model
from tests.utils.fake_client import FakeClient

#: Embeds a double-quote AND a statement terminator - the same shape as the adversarial keys
#: already used in tests/test_indexes.py's PartialIndex.condition coverage.
ADVERSARIAL_IDENTIFIER = 'evil"; DROP TABLE users; --'
ADVERSARIAL_IDENTIFIER_QUOTED = '"evil""; DROP TABLE users; --"'


class SqlSchemaEditorTableComments(TableComments):
    def get_table_comment_sql(self, table: str, comment: str) -> str:
        return ""

    def get_column_comment_sql(self, table: str, column: str, comment: str) -> str:
        return ""


class SqlSchemaEditor(BaseSchemaEditor):
    table_comments_class = SqlSchemaEditorTableComments


def init_apps(*models: type[Model]) -> None:
    apps = StateApps()
    for model in models:
        apps.register_model("models", model)
    apps.init_relations()


# ---------------------------------------------------------------------------
# source_field - column identifiers (FIELD_TEMPLATE / GENERATED_PK_TEMPLATE / FOREIGN_KEY_TEMPLATE)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_model_quotes_adversarial_source_field() -> None:
    """Field.source_field is a free-form str never validated against SQL metacharacters -
    it used to be spliced into FIELD_TEMPLATE's literal '"{name}"' unescaped."""

    class Widget(Model):
        id = fields.IntField(primary_key=True)
        price = fields.IntField(source_field=ADVERSARIAL_IDENTIFIER)

        class Meta:
            table = "widget"
            app = "models"

    init_apps(Widget)
    client = FakeClient("sql")
    editor = SqlSchemaEditor(client)

    await editor.create_model(Widget)

    sql = client.executed[0]
    assert ADVERSARIAL_IDENTIFIER_QUOTED in sql
    assert sql.count('"') % 2 == 0


@pytest.mark.asyncio
async def test_add_field_quotes_adversarial_source_field() -> None:
    class Widget(Model):
        id = fields.IntField(primary_key=True)

        class Meta:
            table = "widget"
            app = "models"

    Widget._meta.fields_map["price"] = fields.IntField(source_field=ADVERSARIAL_IDENTIFIER)
    Widget._meta.fields_map["price"].model_field_name = "price"
    Widget._meta.fields_db_projection["price"] = ADVERSARIAL_IDENTIFIER
    init_apps(Widget)

    client = FakeClient("sql")
    editor = SqlSchemaEditor(client)

    await editor.add_field(Widget, "price")

    sql = client.executed[0]
    assert ADVERSARIAL_IDENTIFIER_QUOTED in sql
    assert sql.count('"') % 2 == 0


@pytest.mark.asyncio
async def test_remove_field_quotes_adversarial_source_field() -> None:
    class Widget(Model):
        id = fields.IntField(primary_key=True)
        price = fields.IntField(source_field=ADVERSARIAL_IDENTIFIER)

        class Meta:
            table = "widget"
            app = "models"

    init_apps(Widget)
    client = FakeClient("sql")
    editor = SqlSchemaEditor(client)

    await editor.remove_field(Widget, Widget._meta.fields_map["price"])

    sql = client.executed[0]
    assert ADVERSARIAL_IDENTIFIER_QUOTED in sql
    assert sql.count('"') % 2 == 0


@pytest.mark.asyncio
async def test_alter_field_rename_quotes_adversarial_old_and_new_source_field() -> None:
    class Widget(Model):
        id = fields.IntField(primary_key=True)
        price = fields.IntField(source_field=ADVERSARIAL_IDENTIFIER)

        class Meta:
            table = "widget"
            app = "models"

    class WidgetRenamed(Model):
        id = fields.IntField(primary_key=True)
        price = fields.IntField(source_field='new_evil"col')

        class Meta:
            table = "widget"
            app = "models"

    init_apps(Widget)
    init_apps(WidgetRenamed)
    client = FakeClient("sql")
    editor = SqlSchemaEditor(client)

    await editor.alter_column(WidgetRenamed, Widget._meta.fields_map["price"], WidgetRenamed._meta.fields_map["price"])

    sql = "\n".join(client.executed)
    assert ADVERSARIAL_IDENTIFIER_QUOTED in sql
    assert '"new_evil""col"' in sql
    assert sql.count('"') % 2 == 0


@pytest.mark.asyncio
async def test_fk_field_quotes_adversarial_target_source_field() -> None:
    """A ForeignKeyField's inline REFERENCES clause names the TARGET column
    (to_field_instance.source_field) - also spliced into FOREIGN_KEY_TEMPLATE's literal '("{field}")'
    unescaped."""

    class Team(Model):
        code = fields.CharField(max_length=10, primary_key=True, source_field=ADVERSARIAL_IDENTIFIER)

        class Meta:
            table = "team"
            app = "models"

    class Membership(Model):
        id = fields.IntField(primary_key=True)
        team = fields.ForeignKeyField("models.Team", related_name="memberships")

        class Meta:
            table = "membership"
            app = "models"

    init_apps(Team, Membership)
    client = FakeClient("sql")
    editor = SqlSchemaEditor(client)

    await editor.create_model(Membership)

    sql = client.executed[0]
    assert ADVERSARIAL_IDENTIFIER_QUOTED in sql
    assert sql.count('"') % 2 == 0


@pytest.mark.asyncio
async def test_create_model_adversarial_source_field_executes_on_real_sqlite() -> None:
    """Beyond textual escaping: the generated DDL must actually create a working table, and the
    adversarially-named column must genuinely be usable for reads/writes against a real DB."""

    class Widget(Model):
        id = fields.IntField(primary_key=True)
        price = fields.IntField(source_field=ADVERSARIAL_IDENTIFIER)

        class Meta:
            table = "widget"
            app = "models"

    init_apps(Widget)
    client = FakeClient("sqlite")
    editor = SqliteSchemaEditor(client, collect_sql=True)

    await editor.create_model(Widget)

    conn = sqlite3.connect(":memory:")
    try:
        for statement in editor.collected_sql:
            conn.executescript(statement)
        conn.execute(f'INSERT INTO "widget" ("id", {ADVERSARIAL_IDENTIFIER_QUOTED}) VALUES (1, 42)')
        row = conn.execute(f'SELECT {ADVERSARIAL_IDENTIFIER_QUOTED} FROM "widget"').fetchone()
        assert row[0] == 42
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# M2M forward_key/backward_key - through-table column identifiers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_m2m_field_quotes_adversarial_forward_and_backward_keys() -> None:
    class Tag(Model):
        id = fields.IntField(primary_key=True)

        class Meta:
            table = "tag"
            app = "models"

    class Widget(Model):
        id = fields.IntField(primary_key=True)
        tags = fields.ManyToManyField(
            Tag,
            related_name="widgets",
            forward_key=ADVERSARIAL_IDENTIFIER,
            backward_key='evil_back"key',
        )

        class Meta:
            table = "widget"
            app = "models"

    init_apps(Tag, Widget)
    client = FakeClient("sql")
    editor = SqlSchemaEditor(client)

    await editor.add_field(Widget, "tags")

    sql = client.executed[0]
    assert ADVERSARIAL_IDENTIFIER_QUOTED in sql
    assert '"evil_back""key"' in sql
    assert sql.count('"') % 2 == 0


@pytest.mark.asyncio
async def test_m2m_field_adversarial_keys_executes_on_real_sqlite() -> None:
    class Tag(Model):
        id = fields.IntField(primary_key=True)

        class Meta:
            table = "tag"
            app = "models"

    class Widget(Model):
        id = fields.IntField(primary_key=True)
        tags = fields.ManyToManyField(
            Tag,
            related_name="widgets",
            forward_key=ADVERSARIAL_IDENTIFIER,
            backward_key='evil_back"key',
        )

        class Meta:
            table = "widget"
            app = "models"

    init_apps(Tag, Widget)
    client = FakeClient("sqlite")
    editor = SqliteSchemaEditor(client, collect_sql=True)

    await editor.create_model(Tag)
    await editor.create_model(Widget)

    conn = sqlite3.connect(":memory:")
    try:
        for statement in editor.collected_sql:
            conn.executescript(statement)
        conn.execute('INSERT INTO "tag" ("id") VALUES (1)')
        conn.execute('INSERT INTO "widget" ("id") VALUES (1)')
        conn.execute(f'INSERT INTO "widget_tag" ("evil_back""key", {ADVERSARIAL_IDENTIFIER_QUOTED}) VALUES (1, 1)')
        row = conn.execute(f'SELECT "evil_back""key", {ADVERSARIAL_IDENTIFIER_QUOTED} FROM "widget_tag"').fetchone()
        assert row == (1, 1)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Constraint names - CHECK/EXCLUDE/UNIQUE constraint templates
# ---------------------------------------------------------------------------


def _product_model() -> type[Model]:
    class Product(Model):
        id = fields.IntField(primary_key=True)
        price = fields.IntField()

        class Meta:
            table = "product"
            app = "models"

    return Product


@pytest.mark.asyncio
async def test_add_check_constraint_quotes_adversarial_name() -> None:
    Product = _product_model()
    init_apps(Product)
    client = FakeClient("sql")
    editor = SqlSchemaEditor(client)

    constraint = CheckConstraint(check=RawSQLTerm("price > 0"), name=ADVERSARIAL_IDENTIFIER)
    await editor.add_constraint(Product, constraint)

    sql = client.executed[0]
    assert ADVERSARIAL_IDENTIFIER_QUOTED in sql
    assert sql.count('"') % 2 == 0


@pytest.mark.asyncio
async def test_remove_check_constraint_quotes_adversarial_name() -> None:
    Product = _product_model()
    init_apps(Product)
    client = FakeClient("sql")
    editor = SqlSchemaEditor(client)

    constraint = CheckConstraint(check=RawSQLTerm("price > 0"), name=ADVERSARIAL_IDENTIFIER)
    await editor.remove_constraint(Product, constraint)

    sql = client.executed[0]
    assert ADVERSARIAL_IDENTIFIER_QUOTED in sql
    assert sql.count('"') % 2 == 0


@pytest.mark.asyncio
async def test_add_check_constraint_adversarial_name_executes_on_real_sqlite() -> None:
    Product = _product_model()
    Product._meta.constraints = [CheckConstraint(check=RawSQLTerm("price > 0"), name=ADVERSARIAL_IDENTIFIER)]
    init_apps(Product)
    client = FakeClient("sqlite")
    editor = SqliteSchemaEditor(client, collect_sql=True)

    await editor.create_model(Product)

    conn = sqlite3.connect(":memory:")
    try:
        for statement in editor.collected_sql:
            conn.executescript(statement)
        conn.execute('INSERT INTO "product" ("id", "price") VALUES (1, 5)')
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute('INSERT INTO "product" ("id", "price") VALUES (2, -1)')
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_add_unique_constraint_quotes_adversarial_explicit_name() -> None:
    Product = _product_model()
    init_apps(Product)
    client = FakeClient("sql")
    editor = SqlSchemaEditor(client)

    constraint = UniqueConstraint(fields=("price",), name=ADVERSARIAL_IDENTIFIER)
    await editor.add_constraint(Product, constraint)

    sql = client.executed[0]
    assert ADVERSARIAL_IDENTIFIER_QUOTED in sql
    assert sql.count('"') % 2 == 0


@pytest.mark.asyncio
async def test_add_exclusion_constraint_quotes_adversarial_name() -> None:
    Product = _product_model()
    init_apps(Product)
    client = FakeClient("postgresql", inline_comment=False)
    editor = PostgresqlSchemaEditor(client)

    constraint = ExclusionConstraint(name=ADVERSARIAL_IDENTIFIER, expressions=(("price", "="),))
    await editor.add_constraint(Product, constraint)

    sql = client.executed[0]
    assert ADVERSARIAL_IDENTIFIER_QUOTED in sql
    assert sql.count('"') % 2 == 0


@pytest.mark.asyncio
async def test_rename_constraint_quotes_adversarial_old_and_new_name() -> None:
    Product = _product_model()
    init_apps(Product)
    client = FakeClient("postgresql", inline_comment=False)
    editor = PostgresqlSchemaEditor(client)

    old = CheckConstraint(check=RawSQLTerm("price > 0"), name=ADVERSARIAL_IDENTIFIER)
    new = CheckConstraint(check=RawSQLTerm("price > 0"), name='renamed_evil"name')
    await editor.rename_constraint(Product, old, new)

    sql = client.executed[0]
    assert ADVERSARIAL_IDENTIFIER_QUOTED in sql
    assert '"renamed_evil""name"' in sql
    assert sql.count('"') % 2 == 0
