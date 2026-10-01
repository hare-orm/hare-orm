"""detect_drift() comparing each column's own definition - type, nullability, FOREIGN KEY
constraint, DEFAULT - against its field."""

from __future__ import annotations

import os

import pytest

from hare import fields
from hare.contrib.test.helpers import hare_test_context
from hare.core.connections import Connections
from hare.ddl import RawSQLTerm
from hare.ddl.constraints import UniqueConstraint
from hare.dialects.postgresql.introspection import PostgresqlIntrospector
from hare.exceptions import IntegrityError
from hare.fields.db_defaults import Now, SqlDefault
from hare.inspectdb.types import ColumnInfo
from hare.migrations.autodetection.autodetector import MigrationAutodetector
from hare.migrations.drift import ColumnDefinitionComparer, detect_drift
from hare.migrations.operations import AlterField
from hare.models import Model

APP_LABEL = "drift_columns"
TEST_DB_URL = os.getenv("HARE_TEST_DB", "sqlite://:memory:")


class DriftColumnAuthor(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
    age = fields.IntField()
    score = fields.IntField(db_default=5)
    created = fields.DatetimeField(db_default=Now())
    created_on = fields.DateField(db_default=Now())
    created_at_time = fields.TimeField(db_default=Now())
    price = fields.DecimalField(max_digits=10, decimal_places=2, db_default=0)
    flag = fields.BooleanField(db_default=True)
    label = fields.CharField(max_length=10, db_default="x")

    class Meta:
        app = APP_LABEL
        table = "drift_column_author"


class DriftColumnBook(Model):
    id = fields.IntField(primary_key=True)
    author: fields.ForeignKeyRelation[DriftColumnAuthor] = fields.ForeignKeyField(
        "drift_columns.DriftColumnAuthor", related_name="books", on_delete=fields.CASCADE
    )
    editor: fields.ForeignKeyNullableRelation[DriftColumnAuthor] = fields.ForeignKeyField(
        "drift_columns.DriftColumnAuthor", related_name="edited", null=True, on_delete=fields.SET_NULL
    )
    reviewer: fields.ForeignKeyNullableRelation[DriftColumnAuthor] = fields.ForeignKeyField(
        "drift_columns.DriftColumnAuthor", related_name="reviewed", null=True, on_delete=fields.PROTECT
    )

    class Meta:
        app = APP_LABEL
        table = "drift_column_book"


class DriftColumnPartialUnique(Model):
    """generate_schemas() creates a conditional UniqueConstraint on SQLite too."""

    id = fields.IntField(primary_key=True)
    code = fields.CharField(max_length=10)
    status = fields.CharField(max_length=10)

    class Meta:
        app = APP_LABEL
        table = "drift_column_partial_unique"
        constraints = [
            UniqueConstraint(
                fields=("code",), name="uq_drift_column_open_code", condition=RawSQLTerm("status = 'open'")
            )
        ]


def _current_state(ctx):
    apps_config = {APP_LABEL: {"models": [], "default_connection": "models"}}
    return MigrationAutodetector(ctx.apps, apps_config)._current_state()


async def _detect_drift_after(ctx, sql: str | None):
    connection = Connections.get("models")
    if sql is not None:
        await connection.execute_script(sql)
    return await detect_drift(connection, _current_state(ctx), [APP_LABEL], schema="public")


def _altered_field_names(result) -> list[tuple[str, str]]:
    return sorted(
        (operation.model_name, operation.name) for operation in result.operations if isinstance(operation, AlterField)
    )


@pytest.mark.asyncio
async def test_detect_drift_reports_nothing_for_columns_fresh_from_generate_schemas():
    async with hare_test_context(
        modules=["tests.migrations.test_drift_column_definitions"],
        db_url=TEST_DB_URL,
        app_label=APP_LABEL,
        connection_label="models",
    ) as ctx:
        result = await _detect_drift_after(ctx, None)

        assert result.has_drift is False
        assert result.mismatched_columns == []
        await DriftColumnPartialUnique.objects.create(code="a", status="open")
        await DriftColumnPartialUnique.objects.create(code="a", status="closed")
        if ctx.db().dialect.supports_unique_constraints:
            with pytest.raises(IntegrityError):
                await DriftColumnPartialUnique.objects.create(code="a", status="open")


POSTGRES_COLUMN_CHANGES = [
    pytest.param(
        "ALTER TABLE drift_column_author ALTER COLUMN name TYPE varchar(10)",
        ("DriftColumnAuthor", "name"),
        id="varchar_length",
    ),
    pytest.param(
        "ALTER TABLE drift_column_author ALTER COLUMN age TYPE text", ("DriftColumnAuthor", "age"), id="int_to_text"
    ),
    pytest.param(
        "ALTER TABLE drift_column_author ALTER COLUMN price TYPE numeric(12,3)",
        ("DriftColumnAuthor", "price"),
        id="numeric_size",
    ),
    pytest.param(
        "ALTER TABLE drift_column_author ALTER COLUMN age DROP NOT NULL", ("DriftColumnAuthor", "age"), id="nullable"
    ),
    pytest.param(
        "ALTER TABLE drift_column_book ALTER COLUMN author_id DROP NOT NULL",
        ("DriftColumnBook", "author"),
        id="fk_nullable",
    ),
    pytest.param(
        "ALTER TABLE drift_column_book ALTER COLUMN editor_id SET NOT NULL",
        ("DriftColumnBook", "editor"),
        id="fk_not_null",
    ),
    pytest.param(
        "ALTER TABLE drift_column_author ALTER COLUMN score DROP DEFAULT",
        ("DriftColumnAuthor", "score"),
        id="default_dropped",
    ),
    pytest.param(
        "ALTER TABLE drift_column_author ALTER COLUMN score SET DEFAULT 9",
        ("DriftColumnAuthor", "score"),
        id="default_changed",
    ),
    pytest.param(
        "ALTER TABLE drift_column_author ALTER COLUMN label SET DEFAULT 'y'",
        ("DriftColumnAuthor", "label"),
        id="string_default_changed",
    ),
    pytest.param(
        "ALTER TABLE drift_column_author ALTER COLUMN flag SET DEFAULT false",
        ("DriftColumnAuthor", "flag"),
        id="boolean_default_changed",
    ),
    pytest.param(
        "ALTER TABLE drift_column_author ALTER COLUMN created DROP DEFAULT",
        ("DriftColumnAuthor", "created"),
        id="now_default_dropped",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("sql", "altered_field"), POSTGRES_COLUMN_CHANGES)
async def test_detect_drift_reports_a_changed_postgres_column(sql, altered_field):
    async with hare_test_context(
        modules=["tests.migrations.test_drift_column_definitions"],
        db_url=TEST_DB_URL,
        app_label=APP_LABEL,
        connection_label="models",
    ) as ctx:
        if Connections.get("models").dialect.name != "postgresql":
            pytest.skip("Postgres ALTER COLUMN")

        result = await _detect_drift_after(ctx, sql)

        assert _altered_field_names(result) == [altered_field]
        assert result.mismatched_columns == []


@pytest.mark.asyncio
@pytest.mark.parametrize("on_delete_sql", ["", "ON DELETE RESTRICT"])
async def test_detect_drift_reports_a_dropped_or_changed_postgres_foreign_key(on_delete_sql):
    async with hare_test_context(
        modules=["tests.migrations.test_drift_column_definitions"],
        db_url=TEST_DB_URL,
        app_label=APP_LABEL,
        connection_label="models",
    ) as ctx:
        connection = Connections.get("models")
        if connection.dialect.name != "postgresql":
            pytest.skip("Postgres ALTER TABLE ... DROP CONSTRAINT")
        [row] = await connection.execute_dicts(
            "SELECT conname FROM pg_constraint WHERE conrelid = 'drift_column_book'::regclass AND contype = 'f' "
            "AND conkey = (SELECT array_agg(attnum) FROM pg_attribute "
            "WHERE attrelid = 'drift_column_book'::regclass AND attname = 'author_id')"
        )
        sql = f'ALTER TABLE drift_column_book DROP CONSTRAINT "{row["conname"]}"'
        if on_delete_sql:
            sql += (
                f'; ALTER TABLE drift_column_book ADD CONSTRAINT "{row["conname"]}" '
                f"FOREIGN KEY (author_id) REFERENCES drift_column_author (id) {on_delete_sql}"
            )

        result = await _detect_drift_after(ctx, sql)

        assert _altered_field_names(result) == [("DriftColumnBook", "author")]


@pytest.mark.asyncio
async def test_detect_drift_reports_a_foreign_key_column_of_another_type():
    """A relation's key column takes its type from the target's key - no field describes a
    different one, so it is reported as a column mismatch."""
    async with hare_test_context(
        modules=["tests.migrations.test_drift_column_definitions"],
        db_url=TEST_DB_URL,
        app_label=APP_LABEL,
        connection_label="models",
    ) as ctx:
        if Connections.get("models").dialect.name != "postgresql":
            pytest.skip("Postgres ALTER COLUMN")

        result = await _detect_drift_after(ctx, "ALTER TABLE drift_column_book ALTER COLUMN editor_id TYPE bigint")

        assert result.operations == []
        assert result.has_drift is True
        [mismatch] = result.mismatched_columns
        assert (mismatch.model_name, mismatch.table, mismatch.column) == (
            "DriftColumnBook",
            "drift_column_book",
            "editor_id",
        )
        assert mismatch.detail == "type is bigint, the model expects integer"


@pytest.mark.asyncio
async def test_detect_drift_reports_changed_sqlite_columns():
    """SQLite has no ALTER COLUMN - the tables are rebuilt by hand with a TEXT age column (another
    type affinity), a changed and a dropped DEFAULT, a nullable foreign key column without its
    FOREIGN KEY constraint and a foreign key with another ON DELETE action."""
    async with hare_test_context(
        modules=["tests.migrations.test_drift_column_definitions"],
        db_url=TEST_DB_URL,
        app_label=APP_LABEL,
        connection_label="models",
    ) as ctx:
        if Connections.get("models").dialect.name != "sqlite":
            pytest.skip("SQLite table rebuild")

        result = await _detect_drift_after(
            ctx,
            "PRAGMA foreign_keys = OFF; "
            'DROP TABLE "drift_column_book"; DROP TABLE "drift_column_author"; '
            'CREATE TABLE "drift_column_author" ("id" INT NOT NULL PRIMARY KEY, "name" VARCHAR(10) NOT NULL, '
            '"age" TEXT NOT NULL, "score" INT NOT NULL DEFAULT 9, "created" TIMESTAMP NOT NULL, '
            '"price" VARCHAR(40) NOT NULL DEFAULT 0, "flag" INT NOT NULL DEFAULT 1, '
            "\"label\" VARCHAR(10) NOT NULL DEFAULT 'x'); "
            'CREATE TABLE "drift_column_book" ("id" INT NOT NULL PRIMARY KEY, "author_id" INT, '
            '"editor_id" INT REFERENCES "drift_column_author" ("id") ON DELETE CASCADE, '
            '"reviewer_id" INT REFERENCES "drift_column_author" ("id") ON DELETE NO ACTION); '
            'CREATE INDEX "drift_book_author" ON "drift_column_book" ("author_id"); '
            'CREATE INDEX "drift_book_editor" ON "drift_column_book" ("editor_id"); '
            'CREATE INDEX "drift_book_reviewer" ON "drift_column_book" ("reviewer_id"); '
            "PRAGMA foreign_keys = ON",
        )

        # An unchanged VARCHAR length has the same TEXT affinity - never reported on SQLite.
        assert _altered_field_names(result) == [
            ("DriftColumnAuthor", "age"),
            ("DriftColumnAuthor", "created"),
            ("DriftColumnAuthor", "score"),
            ("DriftColumnBook", "author"),
            ("DriftColumnBook", "editor"),
        ]
        assert result.mismatched_columns == []


@pytest.mark.parametrize(
    ("declared_type", "reported_type"),
    [
        ("VARCHAR(50)", "character varying(50)"),
        ("INT", "integer"),
        ("SERIAL", "integer"),
        ("BIGINT", "bigint"),
        ("BOOL", "boolean"),
        ("DECIMAL(10,2)", "numeric(10,2)"),
        ("TIMESTAMPTZ", "timestamp with time zone"),
        ("TIMESTAMP", "timestamp without time zone"),
        ("TIMETZ", "time with time zone"),
        ("DOUBLE PRECISION", "double precision"),
        ("FLOAT4", "real"),
        ("CHAR", "character(1)"),
        ("VARCHAR(32)[]", "character varying(32)[]"),
        ("JSONB", "jsonb"),
    ],
)
def test_postgres_type_synonyms_normalize_to_the_reported_name(declared_type, reported_type):
    assert PostgresqlIntrospector.get_canonical_type(declared_type) == reported_type
    assert PostgresqlIntrospector.get_canonical_type(reported_type) == reported_type


def test_postgres_type_outside_the_compared_ones_is_never_compared():
    column = ColumnInfo(name="point", db_type="USER-DEFINED", nullable=True, is_pk=False, is_unique=False)
    column.full_type = "geography(Point,4326)"

    assert PostgresqlIntrospector.get_canonical_type("geography(Point,4326)") is None
    assert not ColumnDefinitionComparer.column_types_differ("postgresql", "geography(Point,4326)", column)


@pytest.mark.parametrize(
    ("declared_type", "observed_type", "differ"),
    [
        ("VARCHAR(50)", "VARCHAR(10)", False),
        ("INT", "INTEGER", False),
        ("BIGINT", "INT", False),
        ("INT", "TEXT", True),
        ("REAL", "DOUBLE PRECISION", False),
        ("JSON_TEXT", "JSON", False),
        ("TIMESTAMP", "TEXT", True),
    ],
)
def test_sqlite_types_are_compared_by_affinity(declared_type, observed_type, differ):
    column = ColumnInfo(name="value", db_type=observed_type, nullable=True, is_pk=False, is_unique=False)

    assert ColumnDefinitionComparer.column_types_differ("sqlite", declared_type, column) is differ


@pytest.mark.parametrize(
    ("declared_default", "observed_default", "differ"),
    [
        (0, 0, False),
        (0, "0.00", False),
        (1.5, 1.5, False),
        (True, True, False),
        (True, 1, False),
        (True, False, True),
        ("x", "x", False),
        ("x", "y", True),
        (5, 9, True),
        (Now(), Now(), False),
        (SqlDefault("gen_random_uuid()"), SqlDefault("gen_random_uuid()"), False),
        (SqlDefault("(1 + 1)"), SqlDefault("1 + 1"), False),
        (SqlDefault("'a'"), SqlDefault("'a'::text"), False),
        (SqlDefault("0"), 0, False),
        (SqlDefault("lower('A')"), SqlDefault("upper('A')"), True),
        (5, None, True),
    ],
)
def test_db_defaults_are_compared_by_fingerprint(declared_default, observed_default, differ):
    field = fields.IntField(db_default=declared_default)
    column = ColumnInfo(name="value", db_type="integer", nullable=False, is_pk=False, is_unique=False)
    if isinstance(observed_default, str) and not isinstance(declared_default, str):
        observed_default = SqlDefault(observed_default)
    column.db_default = observed_default

    assert ColumnDefinitionComparer.db_defaults_differ(field, column, "postgresql") is differ
