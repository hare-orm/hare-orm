"""Tests for introspection-based unique constraint removal across all backends."""

from __future__ import annotations

import pytest

from hare import fields
from hare.ddl.constraints import CheckConstraint, ExclusionConstraint, UniqueConstraint
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
from hare.dialects.base.schema.tables.table_comments import TableComments
from hare.dialects.postgresql.schema.postgresql_schema_editor import PostgresqlSchemaEditor
from hare.dialects.sqlite.schema.sqlite_schema_editor import SqliteSchemaEditor
from hare.exceptions import ConfigurationError, UnSupportedError
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
# FK test models (used by FK resolution tests)
# ---------------------------------------------------------------------------


class Team(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=200)

    class Meta:
        table = "team"
        app = "models"


class Membership(Model):
    id = fields.IntField(primary_key=True)
    team: fields.ForeignKeyRelation[Team] = fields.ForeignKeyField("models.Team", related_name="memberships")
    user_email = fields.CharField(max_length=255)

    class Meta:
        table = "membership"
        app = "models"


init_apps(Team, Membership)


# Each backend's introspection returns a different dict key for the constraint name.
# SQLite uses PRAGMA-based introspection (tested separately below).
#
# Fields: editor_cls, client_kwargs, mock_row, expected_name,
#         expected_drop_sql (introspected name), expected_fallback_sql (uid_ name)
INTROSPECTION_BACKENDS = [
    pytest.param(
        PostgresqlSchemaEditor,
        {"dialect": "postgresql", "inline_comment": False},
        {"conname": "legacy_auto_name"},
        "legacy_auto_name",
        'ALTER TABLE "widget" DROP CONSTRAINT "legacy_auto_name"',
        'ALTER TABLE "widget" DROP CONSTRAINT "uid_widget_email_3d71d7d5e5c0"',
        id="postgres",
    ),
]


@pytest.mark.asyncio
async def test_base_get_unique_constraint_names_from_db_returns_empty() -> None:
    """Base _get_unique_constraint_names_from_db returns [] (no introspection)."""
    client = FakeClient("sql")
    editor = TestSchemaEditor(client)
    result = await editor.constraint_names.get_unique_constraint_names_from_db("widget", ["name"], None)
    assert result == []
    assert len(client.executed) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("editor_cls", "client_kwargs", "mock_row", "expected_name", "_drop", "_fallback"),
    INTROSPECTION_BACKENDS,
)
async def test_introspection_returns_constraint_names(
    editor_cls: type[BaseSchemaEditor],
    client_kwargs: dict,
    mock_row: dict,
    expected_name: str,
    _drop: str,
    _fallback: str,
) -> None:
    """Backend introspection returns the constraint name from mock results."""
    client = MockIntrospectionClient(constraint_names=[mock_row], **client_kwargs)
    editor = editor_cls(client)
    result = await editor.constraint_names.get_unique_constraint_names_from_db("widget", ["email"], None)
    assert result == [expected_name]
    assert len(client.executed) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("editor_cls", "client_kwargs", "mock_row", "expected_name", "expected_drop_sql", "_fallback"),
    INTROSPECTION_BACKENDS,
)
async def test_remove_constraint_uses_introspected_name(
    editor_cls: type[BaseSchemaEditor],
    client_kwargs: dict,
    mock_row: dict,
    expected_name: str,
    expected_drop_sql: str,
    _fallback: str,
) -> None:
    """remove_constraint should use the introspected name instead of uid_."""

    class Widget(Model):
        id = fields.IntField(primary_key=True)
        email = fields.CharField(max_length=255, unique=True)

        class Meta:
            table = "widget"
            app = "models"

    client = MockIntrospectionClient(constraint_names=[mock_row], **client_kwargs)
    editor = editor_cls(client)

    constraint = UniqueConstraint(fields=("email",))
    await editor.remove_constraint(Widget, constraint)

    assert len(client.executed) == 1
    assert client.executed[0] == expected_drop_sql


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "editor_cls",
        "client_kwargs",
        "_mock_row",
        "_expected_name",
        "_drop",
        "expected_fallback_sql",
    ),
    INTROSPECTION_BACKENDS,
)
async def test_remove_constraint_fallback_with_fakeclient(
    editor_cls: type[BaseSchemaEditor],
    client_kwargs: dict,
    _mock_row: dict,
    _expected_name: str,
    _drop: str,
    expected_fallback_sql: str,
) -> None:
    """With FakeClient (no introspection) falls back to deterministic uid_ name."""

    class Widget(Model):
        id = fields.IntField(primary_key=True)
        email = fields.CharField(max_length=255, unique=True)

        class Meta:
            table = "widget"
            app = "models"

    client = FakeClient(**client_kwargs)
    editor = editor_cls(client)

    constraint = UniqueConstraint(fields=("email",))
    await editor.remove_constraint(Widget, constraint)

    assert len(client.executed) == 1
    assert client.executed[0] == expected_fallback_sql


# ---------------------------------------------------------------------------
# SQLite-specific introspection tests (PRAGMA-based, different mock shape)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sqlite_introspection_finds_nothing_without_indexes() -> None:
    """SQLite _get_unique_constraint_names_from_db finds no name on a table with no index."""
    client = FakeClient("sqlite")
    editor = SqliteSchemaEditor(client)
    assert await editor.constraint_names.get_unique_constraint_names_from_db("widget", ["name"], None) == []
    assert len(client.executed) == 0


@pytest.mark.asyncio
async def test_sqlite_introspection_finds_unique_index() -> None:
    """SQLite introspection should find unique index by column using PRAGMA."""
    client = MockIntrospectionClient(
        "sqlite",
        pragma_index_list=[
            {
                "seq": 0,
                "name": "sqlite_autoindex_widget_1",
                "unique": 1,
                "origin": "c",
                "partial": 0,
            },
        ],
        pragma_index_info={
            "sqlite_autoindex_widget_1": [{"seqno": 0, "cid": 1, "name": "email"}],
        },
    )
    editor = SqliteSchemaEditor(client)
    result = await editor.constraint_names.get_unique_constraint_names_from_db("widget", ["email"], None)
    assert result == ["sqlite_autoindex_widget_1"]
    assert len(client.executed) == 0


@pytest.mark.asyncio
async def test_sqlite_remove_constraint_fallback_with_fakeclient() -> None:
    """SQLite with FakeClient falls back to deterministic uid_ name."""

    class Widget(Model):
        id = fields.IntField(primary_key=True)
        email = fields.CharField(max_length=255, unique=True)

        class Meta:
            table = "widget"
            app = "models"

    client = FakeClient("sqlite")
    editor = SqliteSchemaEditor(client)

    constraint = UniqueConstraint(fields=("email",))
    await editor.remove_constraint(Widget, constraint)

    assert len(client.executed) == 1
    assert client.executed[0] == 'DROP INDEX "uid_widget_email_3d71d7d5e5c0"'


# ---------------------------------------------------------------------------
# FK field-to-column resolution tests
# ---------------------------------------------------------------------------

ADD_CONSTRAINT_BACKENDS = [
    pytest.param(
        TestSchemaEditor,
        {"dialect": "sql"},
        'ALTER TABLE "membership" ADD CONSTRAINT "uid_membership_team_id_01af112d7f49" UNIQUE '
        '("team_id", "user_email")',
        id="base",
    ),
    pytest.param(
        PostgresqlSchemaEditor,
        {"dialect": "postgresql", "inline_comment": False},
        'ALTER TABLE "membership" ADD CONSTRAINT "uid_membership_team_id_01af112d7f49" UNIQUE '
        '("team_id", "user_email")',
        id="postgres",
    ),
    pytest.param(
        SqliteSchemaEditor,
        {"dialect": "sqlite"},
        'CREATE UNIQUE INDEX "uid_membership_team_id_01af112d7f49" ON "membership" ("team_id", "user_email");',
        id="sqlite",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("editor_cls", "client_kwargs", "expected_sql"),
    ADD_CONSTRAINT_BACKENDS,
)
async def test_add_constraint_resolves_fk_fields(
    editor_cls: type[BaseSchemaEditor],
    client_kwargs: dict,
    expected_sql: str,
) -> None:
    """add_constraint with FK field name must resolve to DB column name (team_id)."""
    client = FakeClient(**client_kwargs)
    editor = editor_cls(client)

    constraint = UniqueConstraint(fields=("team", "user_email"))
    await editor.add_constraint(Membership, constraint)

    assert len(client.executed) == 1
    assert client.executed[0] == expected_sql


@pytest.mark.asyncio
async def test_add_constraint_idempotent_for_resolved_names() -> None:
    """add_constraint with already-resolved DB column names (team_id) works correctly."""
    client = FakeClient("sql")
    editor = TestSchemaEditor(client)

    constraint = UniqueConstraint(fields=("team_id", "user_email"))
    await editor.add_constraint(Membership, constraint)

    assert len(client.executed) == 1
    assert client.executed[0] == (
        'ALTER TABLE "membership" ADD CONSTRAINT "uid_membership_team_id_01af112d7f49" UNIQUE '
        '("team_id", "user_email")'
    )


REMOVE_CONSTRAINT_FK_BACKENDS = [
    pytest.param(
        PostgresqlSchemaEditor,
        {"dialect": "postgresql", "inline_comment": False},
        {"conname": "test_constraint"},
        ['ALTER TABLE "membership" DROP CONSTRAINT "test_constraint"'],
        id="postgres",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("editor_cls", "client_kwargs", "mock_row", "expected_sqls"),
    REMOVE_CONSTRAINT_FK_BACKENDS,
)
async def test_remove_constraint_resolves_fk_for_introspection(
    editor_cls: type[BaseSchemaEditor],
    client_kwargs: dict,
    mock_row: dict,
    expected_sqls: list[str],
) -> None:
    """remove_constraint with FK field name resolves to DB column for introspection query."""
    client = MockIntrospectionClient(constraint_names=[mock_row], **client_kwargs)
    editor = editor_cls(client)

    constraint = UniqueConstraint(fields=("team", "user_email"))
    await editor.remove_constraint(Membership, constraint)

    assert len(client.executed) == len(expected_sqls)
    assert client.executed == expected_sqls


@pytest.mark.asyncio
async def test_rename_constraint_resolves_fk_fields() -> None:
    """rename_constraint on FK model uses resolved column names."""
    client = FakeClient("sql")
    editor = TestSchemaEditor(client)

    old_constraint = UniqueConstraint(fields=("team", "user_email"), name="old_name")
    new_constraint = UniqueConstraint(fields=("team", "user_email"), name="new_name")
    await editor.rename_constraint(Membership, old_constraint, new_constraint)

    assert len(client.executed) == 1
    assert client.executed[0] == 'ALTER TABLE "membership" RENAME CONSTRAINT "old_name" TO "new_name"'


# ---------------------------------------------------------------------------
# CheckConstraint tests
# ---------------------------------------------------------------------------

CHECK_CONSTRAINT_BACKENDS = [
    pytest.param(
        TestSchemaEditor,
        {"dialect": "sql"},
        'ALTER TABLE "product" ADD CONSTRAINT "ck_price" CHECK (price > 0)',
        id="base",
    ),
    pytest.param(
        PostgresqlSchemaEditor,
        {"dialect": "postgresql", "inline_comment": False},
        'ALTER TABLE "product" ADD CONSTRAINT "ck_price" CHECK (price > 0)',
        id="postgres",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("editor_cls", "client_kwargs", "expected_sql"),
    CHECK_CONSTRAINT_BACKENDS,
)
async def test_add_check_constraint_generates_sql(
    editor_cls: type[BaseSchemaEditor],
    client_kwargs: dict,
    expected_sql: str,
) -> None:
    """add_constraint with CheckConstraint generates correct SQL per backend."""

    class Product(Model):
        id = fields.IntField(primary_key=True)
        price = fields.DecimalField(max_digits=10, decimal_places=2)

        class Meta:
            table = "product"
            app = "models"

    client = FakeClient(**client_kwargs)
    editor = editor_cls(client)

    constraint = CheckConstraint(check=RawSQLTerm("price > 0"), name="ck_price")
    await editor.add_constraint(Product, constraint)

    assert len(client.executed) == 1
    assert client.executed[0] == expected_sql


@pytest.mark.asyncio
async def test_remove_check_constraint_generates_sql() -> None:
    """remove_constraint with CheckConstraint generates DROP CONSTRAINT SQL."""

    class Product(Model):
        id = fields.IntField(primary_key=True)
        price = fields.DecimalField(max_digits=10, decimal_places=2)

        class Meta:
            table = "product"
            app = "models"

    client = FakeClient("sql")
    editor = TestSchemaEditor(client)

    constraint = CheckConstraint(check=RawSQLTerm("price > 0"), name="ck_price")
    await editor.remove_constraint(Product, constraint)

    assert len(client.executed) == 1
    assert client.executed[0] == 'ALTER TABLE "product" DROP CONSTRAINT "ck_price"'


@pytest.mark.asyncio
async def test_sqlite_add_check_constraint_rebuilds_table() -> None:
    """SQLite adds CHECK constraints by rebuilding the table."""

    class Product(Model):
        id = fields.IntField(primary_key=True)
        price = fields.DecimalField(max_digits=10, decimal_places=2)

        class Meta:
            table = "product"
            app = "models"
            constraints = [CheckConstraint(check=RawSQLTerm("price > 0"), name="ck_price")]

    client = FakeClient("sqlite")
    editor = SqliteSchemaEditor(client)

    constraint = CheckConstraint(check=RawSQLTerm("price > 0"), name="ck_price")
    await editor.add_constraint(Product, constraint)

    assert len(client.executed) == 8
    assert client.executed[0] == (
        'CREATE TABLE "new__product" ('
        '"id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL, '
        '"price" VARCHAR(40) NOT NULL, '
        'CONSTRAINT "ck_price" CHECK (price > 0))'
    )
    assert client.executed[1] == (
        'INSERT INTO "new__product" ("id", "price")\n'
        '                SELECT "id", "price"\n'
        '                FROM "product"'
    )
    # The AUTOINCREMENT counter goes on from the replaced table's.
    assert client.executed[2].startswith("UPDATE sqlite_sequence SET seq")
    assert client.executed[3].startswith("INSERT INTO sqlite_sequence")
    assert client.executed[4] == 'DROP TABLE "product"'
    # A view naming the table would fail the modern rename while the table is briefly missing.
    assert client.executed[5:] == [
        "PRAGMA legacy_alter_table = ON",
        'ALTER TABLE "new__product" RENAME TO "product"',
        "PRAGMA legacy_alter_table = OFF",
    ]


# ---------------------------------------------------------------------------
# Partial unique index (condition) tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_postgres_add_constraint_with_condition_generates_partial_index() -> None:
    """PostgreSQL generates CREATE UNIQUE INDEX ... WHERE for partial unique constraints."""

    class User(Model):
        id = fields.IntField(primary_key=True)
        email = fields.CharField(max_length=255)
        is_active = fields.BooleanField(default=True)

        class Meta:
            table = "user"
            app = "models"

    client = FakeClient("postgresql", inline_comment=False)
    editor = PostgresqlSchemaEditor(client)

    constraint = UniqueConstraint(fields=("email",), name="uq_active_email", condition=RawSQLTerm("is_active = true"))
    await editor.add_constraint(User, constraint)

    assert len(client.executed) == 1
    assert client.executed[0] == 'CREATE UNIQUE INDEX "uq_active_email" ON "user" ("email") WHERE is_active = true;'


@pytest.mark.asyncio
async def test_postgres_rename_constraint_with_condition_renames_the_index() -> None:
    """A condition=... UniqueConstraint is materialized as a plain index, not a real table
    constraint - renaming it must emit ALTER INDEX ... RENAME TO, not the base
    ALTER TABLE ... RENAME CONSTRAINT (which fails at runtime: there's no constraint by that
    name, only an index)."""

    class User(Model):
        id = fields.IntField(primary_key=True)
        email = fields.CharField(max_length=255)
        is_active = fields.BooleanField(default=True)

        class Meta:
            table = "user"
            app = "models"

    client = FakeClient("postgresql", inline_comment=False)
    editor = PostgresqlSchemaEditor(client)

    old_constraint = UniqueConstraint(fields=("email",), name="uq_old", condition=RawSQLTerm("is_active = true"))
    new_constraint = UniqueConstraint(fields=("email",), name="uq_new", condition=RawSQLTerm("is_active = true"))
    await editor.rename_constraint(User, old_constraint, new_constraint)

    assert len(client.executed) == 1
    assert client.executed[0] == 'ALTER INDEX "uq_old" RENAME TO "uq_new"'


@pytest.mark.asyncio
async def test_base_add_constraint_with_condition_raises() -> None:
    """Non-PostgreSQL backends raise ConfigurationError for partial unique constraints - the
    same exception type BaseSchemaGenerator's own identical check raises for
    generate_schemas()."""

    class User(Model):
        id = fields.IntField(primary_key=True)
        email = fields.CharField(max_length=255)
        is_active = fields.BooleanField(default=True)

        class Meta:
            table = "user"
            app = "models"

    client = FakeClient("sql")
    editor = TestSchemaEditor(client)

    constraint = UniqueConstraint(fields=("email",), name="uq_active_email", condition=RawSQLTerm("is_active = true"))
    with pytest.raises(UnSupportedError, match="Partial unique indexes"):
        await editor.add_constraint(User, constraint)
    assert len(client.executed) == 0


# ---------------------------------------------------------------------------
# Deferrable UniqueConstraint tests
# ---------------------------------------------------------------------------


def _employee_model() -> type[Model]:
    class Employee(Model):
        id = fields.IntField(primary_key=True)
        email = fields.CharField(max_length=255)

        class Meta:
            table = "employee"
            app = "models"

    return Employee


@pytest.mark.asyncio
async def test_postgres_add_constraint_with_deferrable_generates_ddl() -> None:
    """Postgres appends DEFERRABLE INITIALLY DEFERRED to the UNIQUE constraint clause."""
    Employee = _employee_model()
    client = FakeClient("postgresql", inline_comment=False)
    editor = PostgresqlSchemaEditor(client)

    constraint = UniqueConstraint(
        fields=("email",), name="uq_employee_email", deferrable=True, initially_deferred=True
    )
    await editor.add_constraint(Employee, constraint)

    assert len(client.executed) == 1
    assert client.executed[0] == (
        'ALTER TABLE "employee" ADD CONSTRAINT "uq_employee_email" UNIQUE ("email") DEFERRABLE INITIALLY DEFERRED'
    )


@pytest.mark.asyncio
async def test_postgres_add_constraint_with_deferrable_defaults_to_initially_immediate() -> None:
    """deferrable=True without initially_deferred defaults to INITIALLY IMMEDIATE."""
    Employee = _employee_model()
    client = FakeClient("postgresql", inline_comment=False)
    editor = PostgresqlSchemaEditor(client)

    constraint = UniqueConstraint(fields=("email",), name="uq_employee_email", deferrable=True)
    await editor.add_constraint(Employee, constraint)

    assert len(client.executed) == 1
    assert client.executed[0] == (
        'ALTER TABLE "employee" ADD CONSTRAINT "uq_employee_email" UNIQUE ("email") DEFERRABLE INITIALLY IMMEDIATE'
    )


@pytest.mark.asyncio
async def test_sqlite_add_constraint_with_deferrable_raises() -> None:
    """DEFERRABLE unique constraints are Postgres-only - SQLite has no equivalent feature,
    mirroring SqliteSchemaEditor.add_trigger()'s identical rejection of a deferrable Trigger."""
    Employee = _employee_model()
    client = FakeClient("sqlite")
    editor = SqliteSchemaEditor(client)

    constraint = UniqueConstraint(fields=("email",), name="uq_employee_email", deferrable=True)
    with pytest.raises(UnSupportedError, match="DEFERRABLE unique constraints are not supported"):
        await editor.add_constraint(Employee, constraint)
    assert len(client.executed) == 0


@pytest.mark.asyncio
async def test_base_add_constraint_with_deferrable_raises_on_non_postgres() -> None:
    """The shared ConstraintSchemaEditorMixin.add_constraint() itself also rejects deferrable=True
    for any non-Postgres dialect (defensive, for a future dialect that doesn't fully override
    add_constraint() the way SqliteSchemaEditor does)."""
    Employee = _employee_model()
    client = FakeClient("sql")
    editor = TestSchemaEditor(client)

    constraint = UniqueConstraint(fields=("email",), name="uq_employee_email", deferrable=True)
    with pytest.raises(UnSupportedError, match="DEFERRABLE unique constraints are not supported"):
        await editor.add_constraint(Employee, constraint)
    assert len(client.executed) == 0


@pytest.mark.asyncio
async def test_postgres_add_constraint_deferrable_with_condition_raises() -> None:
    """deferrable=True combined with condition is rejected - a partial unique constraint is
    materialized as a plain index on Postgres, and Postgres has no DEFERRABLE clause for an
    index, only for a real table constraint."""

    class User(Model):
        id = fields.IntField(primary_key=True)
        email = fields.CharField(max_length=255)
        is_active = fields.BooleanField(default=True)

        class Meta:
            table = "user"
            app = "models"

    client = FakeClient("postgresql", inline_comment=False)
    editor = PostgresqlSchemaEditor(client)

    constraint = UniqueConstraint(
        fields=("email",),
        name="uq_active_email",
        condition=RawSQLTerm("is_active = true"),
        deferrable=True,
    )
    with pytest.raises(ConfigurationError, match="deferrable"):
        await editor.add_constraint(User, constraint)
    assert len(client.executed) == 0


# ---------------------------------------------------------------------------
# ExclusionConstraint tests
# ---------------------------------------------------------------------------


def _order_model() -> type[Model]:
    class Order(Model):
        id = fields.IntField(primary_key=True)
        resource = fields.IntField()

        class Meta:
            table = "order"
            app = "models"

    return Order


@pytest.mark.asyncio
async def test_add_exclusion_constraint_generates_sql() -> None:
    Order = _order_model()
    client = FakeClient("postgresql", inline_comment=False)
    editor = PostgresqlSchemaEditor(client)

    constraint = ExclusionConstraint(name="no_overlap", expressions=(("resource", "="), ("during", "&&")))
    await editor.add_constraint(Order, constraint)

    assert len(client.executed) == 1
    assert client.executed[0] == (
        'ALTER TABLE "order" ADD CONSTRAINT "no_overlap" EXCLUDE USING gist ("resource" WITH =, "during" WITH &&)'
    )


@pytest.mark.asyncio
async def test_add_exclusion_constraint_with_condition_generates_where_clause() -> None:
    Order = _order_model()
    client = FakeClient("postgresql", inline_comment=False)
    editor = PostgresqlSchemaEditor(client)

    constraint = ExclusionConstraint(
        name="no_overlap_active",
        expressions=(("resource", "="), ("during", "&&")),
        condition=RawSQLTerm("cancelled = false"),
    )
    await editor.add_constraint(Order, constraint)

    assert len(client.executed) == 1
    assert client.executed[0] == (
        'ALTER TABLE "order" ADD CONSTRAINT "no_overlap_active" EXCLUDE USING gist '
        '("resource" WITH =, "during" WITH &&) WHERE (cancelled = false)'
    )


@pytest.mark.asyncio
async def test_add_exclusion_constraint_with_raw_sql_expression_generates_sql() -> None:
    """A RawSQLTerm expression entry (e.g. a range-typed function call, not a plain column) is
    spliced into the EXCLUDE expression list verbatim instead of being resolved as a field name."""
    Order = _order_model()
    client = FakeClient("postgresql", inline_comment=False)
    editor = PostgresqlSchemaEditor(client)

    constraint = ExclusionConstraint(
        name="no_overlap_range",
        expressions=(
            ("resource", "="),
            (RawSQLTerm("tsrange(start_date, end_date)"), "&&"),
        ),
    )
    await editor.add_constraint(Order, constraint)

    assert len(client.executed) == 1
    assert client.executed[0] == (
        'ALTER TABLE "order" ADD CONSTRAINT "no_overlap_range" EXCLUDE USING gist '
        '("resource" WITH =, tsrange(start_date, end_date) WITH &&)'
    )


@pytest.mark.asyncio
async def test_add_exclusion_constraint_raises_on_sqlite() -> None:
    """ExclusionConstraint is Postgres-only - SQLite has no equivalent feature."""
    Order = _order_model()
    client = FakeClient("sql")
    editor = TestSchemaEditor(client)

    constraint = ExclusionConstraint(name="no_overlap", expressions=(("resource", "="), ("during", "&&")))
    with pytest.raises(UnSupportedError, match="ExclusionConstraint is not supported"):
        await editor.add_constraint(Order, constraint)
    assert len(client.executed) == 0


@pytest.mark.asyncio
async def test_remove_exclusion_constraint_generates_sql() -> None:
    Order = _order_model()
    client = FakeClient("postgresql", inline_comment=False)
    editor = PostgresqlSchemaEditor(client)

    constraint = ExclusionConstraint(name="no_overlap", expressions=(("resource", "="), ("during", "&&")))
    await editor.remove_constraint(Order, constraint)

    assert len(client.executed) == 1
    assert client.executed[0] == 'ALTER TABLE "order" DROP CONSTRAINT "no_overlap"'


@pytest.mark.asyncio
async def test_rename_exclusion_constraint_generates_sql() -> None:
    Order = _order_model()
    client = FakeClient("postgresql", inline_comment=False)
    editor = PostgresqlSchemaEditor(client)

    old_constraint = ExclusionConstraint(name="no_overlap_old", expressions=(("resource", "="), ("during", "&&")))
    new_constraint = ExclusionConstraint(name="no_overlap_new", expressions=(("resource", "="), ("during", "&&")))
    await editor.rename_constraint(Order, old_constraint, new_constraint)

    assert len(client.executed) == 1
    assert client.executed[0] == 'ALTER TABLE "order" RENAME CONSTRAINT "no_overlap_old" TO "no_overlap_new"'
