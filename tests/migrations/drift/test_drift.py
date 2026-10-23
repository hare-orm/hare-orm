"""Tests for hare.migrations.drift.detect_drift() called directly - not through the `hare
drift` CLI command/argparse (see tests/cli/test_drift.py for that end-to-end coverage)."""

from __future__ import annotations

import os

import pytest

from hare import fields
from hare.contrib.test import requires_features
from hare.contrib.test.isolated_contexts import hare_test_context
from hare.core.connections.connections import Connections
from hare.ddl.constraints import CheckConstraint, ExclusionConstraint, UniqueConstraint
from hare.ddl.enums import ExclusionConstraintUsing, TriggerTiming
from hare.ddl.indexes import Index, PartialIndex
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.ddl.schema_objects.trigger import Trigger
from hare.dialects.postgresql.indexes import HnswIndex, IvfflatIndex
from hare.inspectdb.introspection.database_catalog import DatabaseCatalog
from hare.migrations.autodetection.migration_autodetector import MigrationAutodetector
from hare.migrations.drift import DriftResult, detect_drift
from hare.migrations.operations import AddConstraint, AddField, AddIndex, AlterField
from hare.models import Model
from hare.models.enums import ModelOption
from hare.query.expressions import Q
from hare.query.functions import Lower
from hare.vectors import VectorField
from tests.migrations.round_trips.test_round_trip_real_db import APP_LABEL, RoundTrip, build_live_state, build_model
from tests.utils.database_under_test import DatabaseUnderTest


class DriftWidget(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)

    class Meta:
        app = "drift_models"
        table = "drift_widget"


class DriftSourceFieldFkTarget(Model):
    id = fields.IntField(primary_key=True)

    class Meta:
        app = "drift_models"
        table = "drift_source_field_fk_target"


class DriftSourceFieldFkOwner(Model):
    """The FK's own source_field= override (a real DB column name distinct from the field's
    auto-derived shadow attribute name "target_id") used to make detect_drift() report the real
    column as untracked - see test_detect_drift_reports_no_drift_for_a_foreign_key_with_a_
    source_field_override below."""

    id = fields.IntField(primary_key=True)
    target: fields.ForeignKeyNullableRelation[DriftSourceFieldFkTarget] = fields.ForeignKeyField(
        "drift_models.DriftSourceFieldFkTarget", source_field="target_ref", null=True
    )

    class Meta:
        app = "drift_models"
        table = "drift_source_field_fk_owner"


class DriftWidgetInCustomSchema(Model):
    """Meta.schema set to something other than "public" - build_database_state() used to
    introspect every model's table against detect_drift()'s own `schema` argument
    unconditionally, ignoring a model's own Meta.schema entirely. See
    test_detect_drift_respects_a_models_own_meta_schema below.

    A distinct Meta.app ("drift_models_custom_schema", not "drift_models") - on SQLite, where
    Meta.schema is a no-op (CreateSchema/DropSchema don't do anything there, so this model's
    table lands in the same place every OTHER model's does regardless of its own Meta.schema),
    sharing "drift_models" with them made this model's table show up as a false-positive
    "untracked" table in every OTHER test in this file - real (SQLite created it, ignoring the
    schema), but this fix's own per-model schema resolution correctly looks for it in
    "drift_custom" specifically, where SQLite has no such separate concept to find it in. Kept
    out of every other test's own target_labels=["drift_models"] scan entirely instead
    (detect_drift()'s own `if app_label not in target_labels: continue`), even though it's still
    discovered/schema-generated alongside them via the shared `modules=[...]` scan."""

    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)

    class Meta:
        app = "drift_models_custom_schema"
        table = "drift_widget_custom_schema"
        schema = "drift_custom"


class DriftSingleColumnIndexedWidget(Model):
    """A plain ``db_index=True`` field - SchemaIntrospector folds this into
    ColumnInfo.has_index rather than a separate IndexInfo entry (see test below)."""

    id = fields.IntField(primary_key=True)
    slug = fields.CharField(max_length=32, db_index=True)

    class Meta:
        app = "drift_models"
        table = "drift_single_column_indexed_widget"


class DriftWing(Model):
    """A target keyed by two columns."""

    a = fields.IntField()
    b = fields.CharField(max_length=5)
    pk = fields.CompositePrimaryKey("a", "b")

    class Meta:
        app = "drift_models"
        table = "drift_wing"


class DriftRoom(Model):
    """Indexes over a relation of two key columns in every shape - the relation named by itself, its
    columns apart, reversed, descending, in a partial index and a reversed unique constraint: drift
    named the relation twice and failed, or showed the indexes removed and added again."""

    id = fields.IntField(primary_key=True)
    wing = fields.ForeignKeyField("drift_models.DriftWing", related_name="rooms")
    other = fields.ForeignKeyField("drift_models.DriftWing", related_name="other_rooms", null=True)
    w = fields.IntField(default=1)

    class Meta:
        app = "drift_models"
        table = "drift_room"
        indexes = [
            Index(fields=["wing", "w"], name="drift_room_adjacent"),
            Index(fields=["wing_a", "w", "wing_b"], name="drift_room_apart"),
            Index(fields=["wing_b", "wing_a"], name="drift_room_reversed"),
            Index(fields=["-wing_a", "-wing_b"], name="drift_room_descending"),
            Index(fields=["wing", "-w"], name="drift_room_wing_then_descending"),
            Index(fields=["-wing", "w"], name="drift_room_descending_wing"),
            PartialIndex(fields=["wing", "w"], name="drift_room_partial", condition=Q(w__gt=0)),
        ]
        constraints = [UniqueConstraint(fields=("other_b", "other_a"), name="drift_room_other_reversed")]


class DriftIndexedWidget(Model):
    id = fields.IntField(primary_key=True)
    f1 = fields.CharField(max_length=16)
    f2 = fields.CharField(max_length=16)
    u1 = fields.IntField()
    u2 = fields.IntField()

    class Meta:
        app = "drift_models"
        table = "drift_indexed_widget"
        indexes = [Index(fields=["f1", "f2"])]
        constraints = (UniqueConstraint(fields=("u1", "u2")),)


class DriftCheckConstraintWidget(Model):
    """A plain CHECK constraint - dialect-neutral (unlike ExclusionConstraint), so shares the
    "drift_models" app with every sqlite-run test in this file too."""

    id = fields.IntField(primary_key=True)
    price = fields.IntField()

    class Meta:
        app = "drift_models"
        table = "drift_check_constraint_widget"
        constraints = [CheckConstraint(check=RawSQLTerm("price > 0"), name="ck_drift_price_positive")]


class DriftExclusionWidget(Model):
    """A separate Meta.app (not "drift_models") - ExclusionConstraint is Postgres-only, so this
    model's schema must never get generated on a SQLite-targeted context; the sqlite-run tests
    above all scope to app_label="drift_models" and never see this app at all. `using=BTREE`
    avoids needing the btree_gist extension for a plain equality operator."""

    id = fields.IntField(primary_key=True)
    resource = fields.IntField()
    cancelled = fields.BooleanField(default=False)

    class Meta:
        app = "drift_models_exclusion"
        table = "drift_exclusion_widget"
        constraints = [
            ExclusionConstraint(
                name="drift_excl_no_overlap",
                expressions=(("resource", "="),),
                using=ExclusionConstraintUsing.BTREE,
                condition=RawSQLTerm("NOT cancelled"),
            )
        ]


class DriftExpressionIndexWidget(Model):
    """A named, expression-based index (``Index(Lower("name"), name=...)``) - separate Meta.app
    from "drift_models" so the sqlite-run tests never even generate this schema at all: SQLite's
    own introspector can't reconstruct an expression index (surfaced as an unparsed-index comment
    instead), so this scenario is meaningful only on Postgres."""

    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)

    class Meta:
        app = "drift_models_expression_index"
        table = "drift_expression_index_widget"
        indexes = [Index(Lower("name"), name="idx_drift_expr_lower_name")]


class DriftPartialIndexWidget(Model):
    """Partial indexes whose predicate the database prints back its own way: SQLite quotes the
    column ("isbn" = 'x'), Postgres casts a varchar column ((isbn)::text = 'x'::text), and a
    predicate other than equalities is never parsed back at all."""

    id = fields.IntField(primary_key=True)
    isbn = fields.CharField(max_length=20, null=True)
    copies = fields.IntField(null=True)

    class Meta:
        app = "drift_models"
        table = "drift_partial_index_widget"
        indexes = [
            PartialIndex(fields=("isbn",), condition=RawSQLTerm("isbn = 'x'"), name="drift_partial_isbn"),
            PartialIndex(fields=("copies",), condition=RawSQLTerm("copies = 1"), name="drift_partial_copies"),
            PartialIndex(fields=("isbn",), condition=RawSQLTerm("copies IS NOT NULL"), name="drift_partial_raw"),
        ]


class DriftPrimaryKeyIndexWidget(Model):
    """An index of its own on the primary key's column - on SQLite not the primary key's index,
    though it covers the same columns."""

    id = fields.IntField(primary_key=True)
    archived = fields.BooleanField(default=False)

    class Meta:
        app = "drift_models"
        table = "drift_primary_key_index_widget"
        indexes = [PartialIndex(fields=("id",), condition=Q(archived=False), name="drift_live_ids")]


class DriftManyToManyLabel(Model):
    id = fields.IntField(primary_key=True)

    class Meta:
        app = "drift_models"
        table = "drift_m2m_label"


class DriftManyToManyPost(Model):
    id = fields.IntField(primary_key=True)
    labels: fields.ManyToManyRelation[DriftManyToManyLabel] = fields.ManyToManyField(
        "drift_models.DriftManyToManyLabel", related_name="posts", through="drift_m2m_post_label"
    )

    class Meta:
        app = "drift_models"
        table = "drift_m2m_post"


class DriftJsonDocument(Model):
    id = fields.IntField(primary_key=True)
    payload = fields.JSONField(null=True)

    class Meta:
        app = "drift_models"
        table = "drift_json_document"


class DriftSchemaManyToManyPost(Model):
    """Declared before DriftSchemaManyToManyTag, so the drift pass reaches the tag's other schema
    last - the through table check of this model used to run against that schema's table list."""

    id = fields.IntField(primary_key=True)
    tags: fields.ManyToManyRelation[DriftSchemaManyToManyTag] = fields.ManyToManyField(
        "drift_models_schema_m2m.DriftSchemaManyToManyTag", related_name="posts"
    )

    class Meta:
        app = "drift_models_schema_m2m"
        table = "drift_schema_m2m_post"


class DriftSchemaManyToManyTag(Model):
    id = fields.IntField(primary_key=True)

    class Meta:
        app = "drift_models_schema_m2m"
        table = "drift_schema_m2m_tag"
        schema = "drift_other_schema"


class DriftExclusionRawExpressionWidget(Model):
    """Postgres rewrites a raw EXCLUDE expression (int4range(low, high) comes back with its own
    formatting), so only the declared wording can match."""

    id = fields.IntField(primary_key=True)
    low = fields.IntField()
    high = fields.IntField()

    class Meta:
        app = "drift_models_exclusion_raw"
        table = "drift_exclusion_raw_widget"
        constraints = [
            ExclusionConstraint(
                name="drift_excl_raw_range",
                expressions=((RawSQLTerm("int4range(low, high, '[)')"), "&&"),),
                using=ExclusionConstraintUsing.GIST,
            )
        ]


class DriftVectorIndexWidget(Model):
    """pgvector indexes with non-default WITH (...) parameters and an explicitly declared default
    opclass - both used to drift on every run."""

    id = fields.IntField(primary_key=True)
    embedding = VectorField(dimensions=3, null=True)
    other_embedding = VectorField(dimensions=3, null=True)

    class Meta:
        app = "drift_models_vector"
        table = "drift_vector_widget"
        indexes = [
            IvfflatIndex(fields=("embedding",), lists=5, name="drift_vector_ivfflat"),
            HnswIndex(
                fields=("other_embedding",),
                m=8,
                ef_construction=32,
                opclasses=("vector_cosine_ops",),
                name="drift_vector_hnsw",
            ),
            IvfflatIndex(fields=("other_embedding",), lists=7, opclasses=("vector_l2_ops",)),
        ]


def current_state(ctx):
    apps_config = {"drift_models": {"models": [], "default_connection": "models"}}
    return MigrationAutodetector(ctx.apps, apps_config).current_state()


@pytest.mark.sqlite_only
@pytest.mark.asyncio
async def test_detect_drift_reports_no_drift_on_a_synced_schema():
    async with hare_test_context(
        modules=["tests.migrations.drift.test_drift"], app_label="drift_models", connection_label="models"
    ) as ctx:
        connection = Connections.get("models")
        result = await detect_drift(
            connection, current_state(ctx), ["drift_models"], schema=DatabaseUnderTest.get_public_schema()
        )

        assert isinstance(result, DriftResult)
        assert result.has_drift is False
        assert result.operations == []
        assert result.untracked_tables == []
        assert result.untracked_columns == []


@pytest.mark.sqlite_only
@pytest.mark.asyncio
async def test_detect_drift_reports_no_drift_for_the_synced_drift_models():
    """DriftStateBuilder used to synthesize the implicit per-field Index the live model's own
    ModelState.make_from_model() adds for a plain db_index=True field only on the "new_state"
    (live models) side, never on the observed database side - a freshly-applied db_index=True
    field showed permanent, unfixable "Add index" drift. Likewise a plain CheckConstraint of
    Meta.constraints, which the observed side never read, showed as a phantom AddConstraint.

    DriftStateBuilder._expected_columns()/_column_to_field_name() also used to read a relational
    field's source_fields (fields_map keys, e.g. "target_id") as if it were the real database
    column name - for a source_field= override (real column "target_ref") the real column was
    reported as untracked."""
    async with hare_test_context(
        modules=["tests.migrations.drift.test_drift"], app_label="drift_models", connection_label="models"
    ) as ctx:
        connection = Connections.get("models")
        result = await detect_drift(
            connection, current_state(ctx), ["drift_models"], schema=DatabaseUnderTest.get_public_schema()
        )

        assert result.has_drift is False
        assert result.operations == []
        assert result.untracked_columns == []


@pytest.mark.asyncio
async def test_detect_drift_respects_a_models_own_meta_schema():
    """build_database_state() used to introspect every target model's table against
    detect_drift()'s own `schema` argument unconditionally, ignoring a model's own Meta.schema
    entirely - DriftWidgetInCustomSchema (Meta.schema="drift_custom") would have been reported
    as a phantom CreateModel (looked up in "public", where its table genuinely doesn't exist),
    even though its table exists exactly where its own Meta.schema says.

    Postgres-only: Meta.schema is a real, separately-creatable schema there; SQLite has no such
    concept (CreateSchema/DropSchema are no-ops on it), so this fix has nothing to exercise on
    that dialect."""
    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    if not DatabaseUnderTest.get_dialect().features.supports_schemas:
        pytest.skip("Meta.schema is a real, separate concept only on Postgres")
    async with hare_test_context(
        modules=["tests.migrations.drift.test_drift"],
        db_url=db_url,
        app_label="drift_models_custom_schema",
        connection_label="models",
    ) as ctx:
        connection = Connections.get("models")
        apps_config = {"drift_models_custom_schema": {"models": [], "default_connection": "models"}}
        new_state = MigrationAutodetector(ctx.apps, apps_config).current_state()
        result = await detect_drift(
            connection, new_state, ["drift_models_custom_schema"], schema=DatabaseUnderTest.get_public_schema()
        )

        assert result.has_drift is False
        assert result.operations == []
        assert result.untracked_tables == []
        assert result.untracked_columns == []


@pytest.mark.asyncio
async def test_a_model_in_a_schema_not_created_yet_is_reported_missing():
    """detect_drift() raised SchemaNotFoundError for a model whose Meta.schema the database doesn't have
    yet - its model is missing like one whose table isn't there."""
    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    if not DatabaseUnderTest.get_dialect().features.supports_schemas:
        pytest.skip("Meta.schema is a real, separate concept only on Postgres")
    async with hare_test_context(
        modules=["tests.migrations.drift.test_drift"],
        db_url=db_url,
        app_label="drift_models_custom_schema",
        connection_label="models",
    ) as ctx:
        connection = Connections.get("models")
        apps_config = {"drift_models_custom_schema": {"models": [], "default_connection": "models"}}
        new_state = MigrationAutodetector(ctx.apps, apps_config).current_state()
        for model_state in new_state.models.values():
            model_state.options[ModelOption.SCHEMA] = "drift_schema_not_created"
        result = await detect_drift(
            connection, new_state, ["drift_models_custom_schema"], schema=DatabaseUnderTest.get_public_schema()
        )

        assert [type(operation).__name__ for operation in result.operations] == ["CreateSchema", "CreateModel"]


@pytest.mark.asyncio
async def test_a_model_naming_the_default_schema_is_compared():
    """The shadow made its own schema for the default one, then made it again for the model's Meta.schema -
    drift failed with "schema already exists"."""
    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    if not DatabaseUnderTest.get_dialect().features.supports_schemas:
        pytest.skip("Meta.schema is a real, separate concept only on Postgres")
    async with hare_test_context(
        modules=["tests.migrations.drift.test_drift"],
        db_url=db_url,
        app_label="drift_models_custom_schema",
        connection_label="models",
    ) as ctx:
        connection = Connections.get("models")
        apps_config = {"drift_models_custom_schema": {"models": [], "default_connection": "models"}}
        new_state = MigrationAutodetector(ctx.apps, apps_config).current_state()
        for model_state in new_state.models.values():
            model_state.options[ModelOption.SCHEMA] = DatabaseUnderTest.get_public_schema()
        result = await detect_drift(
            connection, new_state, ["drift_models_custom_schema"], schema=DatabaseUnderTest.get_public_schema()
        )

        assert [type(operation).__name__ for operation in result.operations] == ["CreateModel"]


@pytest.mark.sqlite_only
@pytest.mark.asyncio
async def test_detect_drift_reports_untracked_table():
    """A table created via raw SQL - bypassing hare's own model/migration bookkeeping entirely -
    must be reported, without going through the CLI/argparse layer at all."""
    async with hare_test_context(
        modules=["tests.migrations.drift.test_drift"], app_label="drift_models", connection_label="models"
    ) as ctx:
        connection = Connections.get("models")
        await connection.execute_script('CREATE TABLE "untracked_thing" (id INTEGER PRIMARY KEY)')

        result = await detect_drift(
            connection, current_state(ctx), ["drift_models"], schema=DatabaseUnderTest.get_public_schema()
        )

        assert result.has_drift is True
        assert result.untracked_tables == ["untracked_thing"]
        assert result.operations == []
        assert result.untracked_columns == []


@pytest.mark.sqlite_only
@pytest.mark.asyncio
async def test_detect_drift_knows_a_migration_journal_it_is_told_of():
    """A MigrationRecorder(table_name=...) journal of migrations the application runs is no untracked
    table once drift is told of it."""
    from hare.migrations import MigrationRecorder, MigrationRunner

    async with hare_test_context(
        modules=["tests.migrations.drift.test_drift"], app_label="drift_models", connection_label="models"
    ) as ctx:
        connection = Connections.get("models")
        await MigrationRunner(
            connection, recorder=MigrationRecorder(connection, table_name="content_migrations")
        ).ensure_journal()

        result = await detect_drift(
            connection, current_state(ctx), ["drift_models"], schema=DatabaseUnderTest.get_public_schema()
        )
        assert result.untracked_tables == ["content_migrations"]
        result = await detect_drift(
            connection,
            current_state(ctx),
            ["drift_models"],
            schema=DatabaseUnderTest.get_public_schema(),
            journal_tables=["content_migrations"],
        )
        assert result.has_drift is False


@pytest.mark.sqlite_only
@pytest.mark.asyncio
async def test_detect_drift_reports_untracked_column():
    """A column added via raw SQL on an otherwise-tracked table must be reported as untracked,
    distinctly from an untracked table."""
    async with hare_test_context(
        modules=["tests.migrations.drift.test_drift"], app_label="drift_models", connection_label="models"
    ) as ctx:
        connection = Connections.get("models")
        await connection.execute_script('ALTER TABLE "drift_widget" ADD COLUMN extra_column TEXT')

        result = await detect_drift(
            connection, current_state(ctx), ["drift_models"], schema=DatabaseUnderTest.get_public_schema()
        )

        assert result.has_drift is True
        assert result.untracked_columns == [("drift_models", "DriftWidget", "drift_widget", "extra_column")]
        assert result.untracked_tables == []
        assert result.operations == []


@pytest.mark.sqlite_only
@pytest.mark.asyncio
async def test_detect_drift_reports_missing_field_as_add_field_operation():
    """A model field with no matching database column shows up as a real AddField Operation -
    the same operation makemigrations would write - not just a plain-text report."""
    async with hare_test_context(
        modules=["tests.migrations.drift.test_drift"], app_label="drift_models", connection_label="models"
    ) as ctx:
        connection = Connections.get("models")
        new_state = current_state(ctx)
        # Add a field to the in-memory State only (not the real, already-created table) - the
        # same shape of drift a model change that was never migrated would produce.
        new_state.models[("drift_models", "DriftWidget")].fields["description"] = fields.CharField(
            max_length=100, null=True
        )

        result = await detect_drift(
            connection, new_state, ["drift_models"], schema=DatabaseUnderTest.get_public_schema()
        )

        assert result.has_drift is True
        assert len(result.operations) == 1
        operation = result.operations[0]
        assert isinstance(operation, AddField)
        assert "description" in operation.describe()
        assert result.untracked_tables == []
        assert result.untracked_columns == []


async def _drop_index_on_columns(connection, table: str, columns: list[str], *, unique: bool) -> None:
    """Finds and drops the real (auto-named) index backing `columns` on `table` - the multi-column
    Meta.indexes/unique_together entries below never carry an explicit name, so the only way to
    target them with raw SQL is by their actual column set."""
    index_rows = await connection.execute_dicts(f'PRAGMA index_list("{table}")')
    for index_row in index_rows:
        if bool(index_row["unique"]) != unique:
            continue
        column_rows = await connection.execute_dicts(f'PRAGMA index_info("{index_row["name"]}")')
        if [row["name"] for row in column_rows] == columns:
            await connection.execute_script(f'DROP INDEX "{index_row["name"]}"')
            return
    raise AssertionError(f"No {'unique ' if unique else ''}index on {columns} found on {table!r}")


@pytest.mark.sqlite_only
@pytest.mark.asyncio
async def test_detect_drift_reports_dropped_multi_column_index():
    """A Meta.indexes multi-column index dropped directly against the database (bypassing
    migrations) must be reported as drift - regression test for a bug where
    DriftStateBuilder.build_database_state() copied the live model's own `options` dict BY
    REFERENCE into the "database state" it's supposed to reconstruct from real introspection, so
    the diff compared a model's declared indexes against itself and could never detect a
    dropped/altered index, unique_together entry, constraint, or trigger on an already-tracked
    table."""
    async with hare_test_context(
        modules=["tests.migrations.drift.test_drift"], app_label="drift_models", connection_label="models"
    ) as ctx:
        connection = Connections.get("models")
        await _drop_index_on_columns(connection, "drift_indexed_widget", ["f1", "f2"], unique=False)

        result = await detect_drift(
            connection, current_state(ctx), ["drift_models"], schema=DatabaseUnderTest.get_public_schema()
        )

        assert result.has_drift is True
        assert any(isinstance(operation, AddIndex) for operation in result.operations)
        assert result.untracked_tables == []
        assert result.untracked_columns == []


async def _drop_unique_constraint(connection, table: str) -> None:
    """An unnamed UniqueConstraint is created on SQLite as a unique index of its generated name -
    dropping that index removes the constraint."""
    rows = await connection.execute_dicts(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = ? AND sql LIKE 'CREATE UNIQUE INDEX%'",
        [table],
    )
    assert rows, f"No unique index found on {table}"
    for row in rows:
        await connection.execute_script(f'DROP INDEX "{row["name"]}"')


@pytest.mark.sqlite_only
@pytest.mark.asyncio
async def test_detect_drift_reports_dropped_unique_constraint():
    """A UniqueConstraint dropped directly against the database must be reported as drift - same
    regression as test_detect_drift_reports_dropped_multi_column_index, covering the unique
    constraint reconstruction path instead of Meta.indexes."""
    async with hare_test_context(
        modules=["tests.migrations.drift.test_drift"], app_label="drift_models", connection_label="models"
    ) as ctx:
        connection = Connections.get("models")
        await _drop_unique_constraint(connection, "drift_indexed_widget")

        result = await detect_drift(
            connection, current_state(ctx), ["drift_models"], schema=DatabaseUnderTest.get_public_schema()
        )

        assert result.has_drift is True
        assert any(isinstance(operation, AddConstraint) for operation in result.operations)
        assert result.untracked_tables == []
        assert result.untracked_columns == []


@pytest.mark.asyncio
async def test_detect_drift_reports_no_drift_for_a_synced_expression_index():
    """A named, expression-based index (Index(Lower("name"), name=...)) after a fresh apply must
    report no drift at all - detect_drift() used to crash with "RemoveIndex requires name or
    fields" for this exact shape (the reconstructed observed index lost its name entirely, since
    IndexInfo never carried it back from introspection), and even once that's fixed, comparing
    the reconstructed RawSQLTerm expression against the live model's own resolved Term by repr()
    reported permanent, unfixable remove+add drift for every expression index, named or not."""
    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    if DatabaseUnderTest.get_dialect().name != "postgresql":
        pytest.skip("Expression-based index reconstruction is Postgres-only")
    async with hare_test_context(
        modules=["tests.migrations.drift.test_drift"],
        db_url=db_url,
        app_label="drift_models_expression_index",
        connection_label="models",
    ) as ctx:
        connection = Connections.get("models")
        apps_config = {"drift_models_expression_index": {"models": [], "default_connection": "models"}}
        new_state = MigrationAutodetector(ctx.apps, apps_config).current_state()
        result = await detect_drift(
            connection, new_state, ["drift_models_expression_index"], schema=DatabaseUnderTest.get_public_schema()
        )

        assert result.has_drift is False
        assert result.operations == []


@pytest.mark.asyncio
async def test_detect_drift_ignores_postgres_canonicalized_exclusion_condition_text():
    """Postgres re-serializes a raw EXCLUDE ... WHERE predicate through its own expression-tree
    pretty-printer (pg_get_constraintdef()) - e.g. the declared condition=RawSQLTerm("NOT cancelled") round-
    trips as "((NOT cancelled))", an extra layer of parens the introspector's regex can't strip
    away (and a more complex predicate can pick up further rewrites, like `!=` normalized to
    `<>` or an added explicit type cast). Comparing that text directly against the user's own
    hand-written condition string used to report permanent, un-fixable drift on this constraint
    on every single run, even immediately after a fresh `hare migrate` with nothing yet changed.

    Postgres-only - ExclusionConstraint/EXCLUDE has no SQLite equivalent at all."""
    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    if DatabaseUnderTest.get_dialect().name != "postgresql":
        pytest.skip("ExclusionConstraint/EXCLUDE is Postgres-only")
    async with hare_test_context(
        modules=["tests.migrations.drift.test_drift"],
        db_url=db_url,
        app_label="drift_models_exclusion",
        connection_label="models",
    ) as ctx:
        connection = Connections.get("models")
        apps_config = {"drift_models_exclusion": {"models": [], "default_connection": "models"}}
        new_state = MigrationAutodetector(ctx.apps, apps_config).current_state()
        result = await detect_drift(
            connection, new_state, ["drift_models_exclusion"], schema=DatabaseUnderTest.get_public_schema()
        )

        assert result.has_drift is False
        assert result.operations == []


@pytest.mark.sqlite_only
@pytest.mark.asyncio
async def test_detect_drift_reports_no_drift_for_synced_partial_indexes():
    """A partial index whose predicate came back as '"isbn" = \'x\'' (SQLite) or
    "((isbn)::text = 'x'::text)" (Postgres) was left unparsed and rebuilt without its condition,
    reporting a Remove/Add index pair on every run."""
    async with hare_test_context(
        modules=["tests.migrations.drift.test_drift"], app_label="drift_models", connection_label="models"
    ) as ctx:
        connection = Connections.get("models")
        result = await detect_drift(
            connection, current_state(ctx), ["drift_models"], schema=DatabaseUnderTest.get_public_schema()
        )

        assert result.operations == []


@pytest.mark.sqlite_only
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "through_table_ddl",
    [
        None,
        "CREATE TABLE drift_m2m_post_label (post_id INT, label_id INT)",
        "CREATE TABLE drift_m2m_post_label (drift_m2m_post_id INT, driftmanytomanylabel_id INT)",
    ],
)
async def test_detect_drift_reports_a_missing_or_broken_many_to_many_through_table(through_table_ddl):
    """A dropped (or recreated without its UNIQUE key) auto-managed through table used to be
    invisible to drift detection - the relation then failed at runtime."""
    async with hare_test_context(
        modules=["tests.migrations.drift.test_drift"], app_label="drift_models", connection_label="models"
    ) as ctx:
        connection = Connections.get("models")
        await connection.execute_script("DROP TABLE drift_m2m_post_label")
        if through_table_ddl is not None:
            await connection.execute_script(through_table_ddl)

        result = await detect_drift(
            connection, current_state(ctx), ["drift_models"], schema=DatabaseUnderTest.get_public_schema()
        )

        assert result.has_drift is True
        assert [
            (type(operation), operation.model_name, operation.name)
            for operation in result.operations
            if isinstance(operation, AddField)
        ] == [(AddField, "DriftManyToManyPost", "labels")]
        assert result.untracked_tables == []


@pytest.mark.sqlite_only
@pytest.mark.asyncio
async def test_detect_drift_reports_a_json_column_of_the_older_sqlite_type():
    """JSONField's SQLite column type changed from JSON to JSON_TEXT - a table created with the older type
    has a column of another type (another affinity), whose field is altered."""
    async with hare_test_context(
        modules=["tests.migrations.drift.test_drift"], app_label="drift_models", connection_label="models"
    ) as ctx:
        connection = Connections.get("models")
        if connection.dialect.name != "sqlite":
            pytest.skip("SQLite column type only")
        await connection.execute_script(
            'DROP TABLE "drift_json_document"; '
            'CREATE TABLE "drift_json_document" ('
            '"id" INTEGER CONSTRAINT "pk_drift_json__id_bf1f65763042" PRIMARY KEY AUTOINCREMENT NOT NULL, '
            '"payload" JSON)'
        )

        result = await detect_drift(
            connection, current_state(ctx), ["drift_models"], schema=DatabaseUnderTest.get_public_schema()
        )

        assert [(type(operation).__name__, operation.name) for operation in result.operations] == [
            ("AlterField", "payload")
        ]


def _postgres_test_database_url_or_skip() -> str:
    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    if DatabaseUnderTest.get_dialect().name != "postgresql":
        pytest.skip("Postgres-only drift scenario")
    return db_url


@pytest.mark.parametrize(
    "app_label",
    [
        # The through-table check reused the table list of the last model the first pass visited -
        # here the tag's other schema - so the post's through table looked missing and drift
        # reported a phantom AddField for the relation.
        pytest.param(
            "drift_models_schema_m2m", id="detect_drift_checks_a_many_to_many_through_table_in_its_owner_schema"
        ),
        # A raw SQL EXCLUDE expression came back rewritten by Postgres and never equalled the
        # declared RawSQLTerm - a Remove/Add constraint pair on every run.
        pytest.param(
            "drift_models_exclusion_raw",
            id="detect_drift_trusts_the_declared_raw_expression_of_an_exclusion_constraint",
        ),
    ],
)
@pytest.mark.asyncio
async def test_detect_drift_reports_no_operation_for_a_postgres_only_shape(app_label):
    db_url = _postgres_test_database_url_or_skip()
    async with hare_test_context(
        modules=["tests.migrations.drift.test_drift"],
        db_url=db_url,
        app_label=app_label,
        connection_label="models",
    ) as ctx:
        connection = Connections.get("models")
        apps_config = {app_label: {"models": [], "default_connection": "models"}}
        new_state = MigrationAutodetector(ctx.apps, apps_config).current_state()
        result = await detect_drift(connection, new_state, [app_label], schema=DatabaseUnderTest.get_public_schema())

        assert result.operations == []


@pytest.mark.asyncio
async def test_detect_drift_reads_vector_index_storage_parameters():
    """The introspector never read an index's WITH (...) parameters, so an IvfflatIndex/HnswIndex
    with non-default lists/m/ef_construction was rebuilt with the class defaults - and a declared
    default opclass (reported by the database as no opclass) never matched either."""
    db_url = _postgres_test_database_url_or_skip()
    try:
        context_manager = hare_test_context(
            modules=["tests.migrations.drift.test_drift"],
            db_url=db_url,
            app_label="drift_models_vector",
            connection_label="models",
        )
        ctx = await context_manager.__aenter__()
    except Exception as error:  # noqa: BLE001 - only a missing pgvector extension is skipped
        if 'extension "vector"' in str(error):
            pytest.skip(f"pgvector is not available: {error}")
        raise
    try:
        connection = Connections.get("models")
        table_info = await DatabaseCatalog.inspect_table(connection, "drift_vector_widget")
        storage_parameters_by_name = {index.name: index.storage_parameters for index in table_info.indexes}
        assert storage_parameters_by_name["drift_vector_ivfflat"] == {"lists": "5"}

        apps_config = {"drift_models_vector": {"models": [], "default_connection": "models"}}
        new_state = MigrationAutodetector(ctx.apps, apps_config).current_state()
        result = await detect_drift(
            connection, new_state, ["drift_models_vector"], schema=DatabaseUnderTest.get_public_schema()
        )

        assert result.operations == []
    finally:
        await context_manager.__aexit__(None, None, None)


def _build_unique_drift_widget(**meta_overrides):
    """UniqueConstraints and unique indexes of every shape the introspector folds or renames."""
    meta_options = {
        "constraints": [
            UniqueConstraint(fields=("code",), name="uq_udw_code"),
            UniqueConstraint(fields=("a", "b"), name="uq_udw_ab"),
            UniqueConstraint(fields=("c",)),
        ],
        "indexes": [
            Index(fields=("label",), unique=True, name="uidx_udw_label"),
            Index(fields=("a", "c"), unique=True),
            Index(RawSQLTerm('LOWER("label")')),
        ],
        **meta_overrides,
    }
    return build_model(
        "UniqueDriftWidget",
        "unique_drift_widget",
        {
            "code": fields.CharField(max_length=20),
            "label": fields.CharField(max_length=20),
            "a": fields.IntField(),
            "b": fields.IntField(),
            "c": fields.IntField(),
            "e": fields.IntField(unique=True),
        },
        meta_options,
    )


@pytest.mark.asyncio
async def test_detect_drift_matches_unique_constraints_and_unique_indexes_to_their_declarations(db_isolated_no_schema):
    """A single-column UniqueConstraint and every Index(unique=True) came back as a unique=True field
    or a unique_together entry, and an unnamed expression index was never matched by its generated
    name - a false Alter field/Add constraint/Add index on a schema fresh out of `migrate`."""
    round_trip = RoundTrip(db_isolated_no_schema.get_connection())
    widget = _build_unique_drift_widget()
    await round_trip.migrate_to(widget)

    result = await detect_drift(round_trip.connection, build_live_state(widget), [APP_LABEL])

    assert result.operations == []


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_detect_drift_still_reports_a_dropped_single_column_unique_constraint(db_isolated_no_schema):
    round_trip = RoundTrip(db_isolated_no_schema.get_connection())
    widget = _build_unique_drift_widget()
    await round_trip.migrate_to(widget)
    if round_trip.dialect == "sqlite":
        await round_trip.connection.execute_script('DROP INDEX "uq_udw_code"; DROP INDEX "uidx_udw_label"')
    else:
        await round_trip.connection.execute_script(
            'ALTER TABLE "unique_drift_widget" DROP CONSTRAINT "uq_udw_code"; DROP INDEX "uidx_udw_label"'
        )

    result = await detect_drift(round_trip.connection, build_live_state(widget), [APP_LABEL])

    assert sorted(type(operation).__name__ for operation in result.operations) == ["AddConstraint", "AddIndex"]
    (add_index,) = [operation for operation in result.operations if isinstance(operation, AddIndex)]
    (add_constraint,) = [operation for operation in result.operations if isinstance(operation, AddConstraint)]
    assert add_index.index.name == "uidx_udw_label"
    assert add_constraint.constraint == UniqueConstraint(fields=("code",), name="uq_udw_code")


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_detect_drift_reports_a_renamed_unique_constraint_as_its_removal_and_addition(
    db_isolated_no_schema,
):
    """The database's constraint and the declared one are told apart by their names - no rename is
    guessed."""
    round_trip = RoundTrip(db_isolated_no_schema.get_connection())
    await round_trip.migrate_to(_build_unique_drift_widget())
    renamed_widget = _build_unique_drift_widget(
        constraints=[
            UniqueConstraint(fields=("code",), name="uq_udw_code_renamed"),
            UniqueConstraint(fields=("a", "b"), name="uq_udw_ab"),
            UniqueConstraint(fields=("c",)),
        ]
    )

    result = await detect_drift(round_trip.connection, build_live_state(renamed_widget), [APP_LABEL])

    assert [type(operation).__name__ for operation in result.operations] == ["RemoveConstraint", "AddConstraint"]
    remove_constraint, add_constraint = result.operations
    assert remove_constraint.name == "uq_udw_code"
    assert add_constraint.constraint.name == "uq_udw_code_renamed"


@pytest.mark.asyncio
async def test_detect_drift_matches_an_inlined_sqlite_unique_constraint_by_its_declared_name(db_isolated_no_schema):
    """A table with a CheckConstraint gets its UniqueConstraints inlined into CREATE TABLE, backed
    by sqlite_autoindex_* indexes whose names never equalled the declared ones - a false
    Remove/Add pair for every one of them."""
    round_trip = RoundTrip(db_isolated_no_schema.get_connection())
    if round_trip.dialect != "sqlite":
        pytest.skip("Inlined UNIQUE constraints are SQLite-specific")
    meta_options = {
        "constraints": [
            CheckConstraint(check=RawSQLTerm("a >= 0"), name="ck_inline_a"),
            UniqueConstraint(fields=("a", "b"), name="uq_inline_ab"),
            UniqueConstraint(fields=("b",), name="uq_inline_b"),
        ]
    }
    widget = build_model(
        "InlineUniqueWidget", "inline_unique_widget", {"a": fields.IntField(), "b": fields.IntField()}, meta_options
    )
    await round_trip.migrate_to(widget)

    result = await detect_drift(round_trip.connection, build_live_state(widget), [APP_LABEL])

    assert result.operations == []


@pytest.mark.asyncio
async def test_detect_drift_matches_conditional_and_deferrable_unique_constraints(db_isolated_no_schema):
    """A conditional UniqueConstraint came back as a PartialIndex (a Remove index/Add constraint
    pair), and a constraint's DEFERRABLE flags were never read, so a changed one went unseen."""
    round_trip = RoundTrip(db_isolated_no_schema.get_connection())
    if round_trip.dialect == "sqlite":
        pytest.skip("Conditional and deferrable unique constraints are Postgres-only")
    widget = build_model(
        "ConditionalUniqueWidget",
        "conditional_unique_widget",
        {"a": fields.IntField(), "b": fields.IntField(), "c": fields.IntField(), "deleted": fields.BooleanField()},
        {
            "constraints": [
                UniqueConstraint(fields=("a", "b"), name="uq_cuw_ab_live", condition=RawSQLTerm("deleted = false")),
                UniqueConstraint(fields=("c",), name="uq_cuw_c", deferrable=True, initially_deferred=True),
            ]
        },
    )
    await round_trip.migrate_to(widget)

    assert (await detect_drift(round_trip.connection, build_live_state(widget), [APP_LABEL])).operations == []

    await round_trip.connection.execute_script(
        'ALTER TABLE "conditional_unique_widget" DROP CONSTRAINT "uq_cuw_c"; '
        'ALTER TABLE "conditional_unique_widget" ADD CONSTRAINT "uq_cuw_c" UNIQUE ("c")'
    )
    result = await detect_drift(round_trip.connection, build_live_state(widget), [APP_LABEL])

    assert [type(operation).__name__ for operation in result.operations] == ["RemoveConstraint", "AddConstraint"]
    assert result.operations[1].constraint.deferrable is True


@pytest.mark.asyncio
async def test_detect_drift_reports_a_changed_predicate_of_a_conditional_unique_constraint(db_isolated_no_schema):
    round_trip = RoundTrip(db_isolated_no_schema.get_connection())
    if round_trip.dialect == "sqlite":
        pytest.skip("Conditional unique constraints are Postgres-only")
    widget = build_model(
        "ConditionalUniqueWidget",
        "conditional_unique_widget",
        {"a": fields.IntField(), "deleted": fields.BooleanField()},
        {
            "constraints": [
                UniqueConstraint(fields=("a",), name="uq_cuw_a_live", condition=RawSQLTerm("deleted = false"))
            ]
        },
    )
    await round_trip.migrate_to(widget)
    await round_trip.connection.execute_script(
        'DROP INDEX "uq_cuw_a_live"; '
        'CREATE UNIQUE INDEX "uq_cuw_a_live" ON "conditional_unique_widget" ("a") WHERE deleted = true'
    )

    result = await detect_drift(round_trip.connection, build_live_state(widget), [APP_LABEL])

    assert sorted(type(operation).__name__ for operation in result.operations) == ["AddConstraint", "RemoveConstraint"]


def _build_trigger_drift_widget(dialect: str, body: str | None = None):
    """A trigger with an UPDATE OF column list, a WHEN condition and an indented multi-line body."""
    if dialect == "sqlite":
        declared_body = body or "\n            UPDATE trigger_drift_widget SET m = 1 WHERE id = NEW.id;\n        "
        timing = TriggerTiming.AFTER
    else:
        declared_body = body or (
            "\n            IF NEW.n > 10 THEN\n                NEW.m := 1;\n"
            "            END IF;\n            RETURN NEW;\n"
        )
        timing = TriggerTiming.BEFORE
    trigger = Trigger(
        name="trg_drift_widget",
        on="UPDATE OF n",
        timing=timing,
        when=RawSQLTerm("NEW.n > 5"),
        body=RawSQLTerm(declared_body),
    )
    return build_model(
        "TriggerDriftWidget",
        "trigger_drift_widget",
        {"n": fields.IntField(default=0), "m": fields.IntField(default=0)},
        {"triggers": (trigger,)},
    )


@pytest.mark.asyncio
async def test_detect_drift_uses_the_declared_wording_of_a_reserialized_trigger(db_isolated_no_schema):
    """The introspector uppercased the UPDATE OF column, Postgres rewrites a WHEN condition as
    "(new.n > 5)", and the body comes back without its surrounding whitespace - a permanent Alter
    trigger for a trigger nobody touched."""
    round_trip = RoundTrip(db_isolated_no_schema.get_connection())
    widget = _build_trigger_drift_widget(round_trip.dialect)
    await round_trip.migrate_to(widget)

    result = await detect_drift(round_trip.connection, build_live_state(widget), [APP_LABEL])

    assert result.operations == []


@pytest.mark.asyncio
async def test_detect_drift_still_reports_a_changed_trigger_body(db_isolated_no_schema):
    round_trip = RoundTrip(db_isolated_no_schema.get_connection())
    await round_trip.migrate_to(_build_trigger_drift_widget(round_trip.dialect))
    changed_body = (
        "UPDATE trigger_drift_widget SET m = 2 WHERE id = NEW.id;"
        if round_trip.dialect == "sqlite"
        else "RETURN NULL;"
    )
    changed_widget = _build_trigger_drift_widget(round_trip.dialect, changed_body)

    result = await detect_drift(round_trip.connection, build_live_state(changed_widget), [APP_LABEL])

    assert [type(operation).__name__ for operation in result.operations] == ["AlterTrigger"]


@pytest.mark.asyncio
async def test_detect_drift_reports_a_changed_type_of_a_single_column_primary_key(db_isolated_no_schema):
    """A single primary key column was taken as declared - its type changed in the database went
    unseen."""
    round_trip = RoundTrip(db_isolated_no_schema.get_connection())
    await round_trip.migrate_to(
        build_model("KeyTypeWidget", "key_type_widget", {"id": fields.CharField(max_length=20, primary_key=True)})
    )
    widget = build_model("KeyTypeWidget", "key_type_widget", {})

    result = await detect_drift(round_trip.connection, build_live_state(widget), [APP_LABEL])

    (alter_field,) = result.operations
    assert isinstance(alter_field, AlterField)
    assert alter_field.name == "id"
    assert isinstance(alter_field.field, fields.IntField)


@pytest.mark.asyncio
async def test_detect_drift_reports_no_drift_for_a_generated_or_typed_primary_key(db_isolated_no_schema):
    round_trip = RoundTrip(db_isolated_no_schema.get_connection())
    widgets = (
        build_model("GeneratedKeyWidget", "generated_key_widget", {}),
        build_model("BigKeyWidget", "big_key_widget", {"id": fields.BigIntField(primary_key=True)}),
        build_model("TextKeyWidget", "text_key_widget", {"id": fields.CharField(max_length=20, primary_key=True)}),
        build_model("UuidKeyWidget", "uuid_key_widget", {"id": fields.UUIDField(primary_key=True)}),
    )
    await round_trip.migrate_to(*widgets)

    result = await detect_drift(round_trip.connection, build_live_state(*widgets), [APP_LABEL])

    assert result.operations == []


@pytest.mark.asyncio
async def test_detect_drift_reports_a_comment_changed_in_the_database(db_isolated_no_schema):
    """The database's comments were never read: the observed fields and table carried the declared
    descriptions, so a comment changed by hand was no drift."""
    from hare.fields import CharField, IntField
    from hare.migrations.operations import AlterField, AlterModelOptions
    from tests.migrations.round_trips.test_round_trip_real_db import (
        APP_LABEL,
        RoundTrip,
        build_live_state,
        build_model,
    )

    round_trip = RoundTrip(db_isolated_no_schema.get_connection())
    if round_trip.dialect != "postgresql":
        pytest.skip("SQLite keeps no column comments")
    model = build_model(
        "Commented",
        "drift_commented",
        {"id": IntField(primary_key=True, generated=False), "code": CharField(max_length=20, description="The code")},
        {"table_description": "Codes"},
    )
    await round_trip.migrate_to(model)

    async def get_drift_operations() -> list[type]:
        drift = await detect_drift(round_trip.connection, build_live_state(model), [APP_LABEL])
        return [type(operation) for operation in drift.operations]

    assert await get_drift_operations() == []
    await round_trip.connection.execute_script("COMMENT ON COLUMN drift_commented.code IS 'Changed by hand'")
    assert await get_drift_operations() == [AlterField]
    await round_trip.connection.execute_script(
        "COMMENT ON COLUMN drift_commented.code IS 'The code'; COMMENT ON TABLE drift_commented IS NULL"
    )
    assert await get_drift_operations() == [AlterModelOptions]


@pytest.mark.asyncio
async def test_detect_drift_reports_a_dropped_primary_key(db_isolated_no_schema):
    """A primary key column's own constraint was never compared: a table whose PRIMARY KEY was
    dropped showed no drift."""
    from hare.fields import IntField
    from tests.migrations.round_trips.test_round_trip_real_db import (
        APP_LABEL,
        RoundTrip,
        build_live_state,
        build_model,
    )

    round_trip = RoundTrip(db_isolated_no_schema.get_connection())
    if round_trip.dialect != "postgresql":
        pytest.skip("SQLite drops a primary key only by rebuilding the table")
    model = build_model("Keyed", "drift_keyed", {"id": IntField(primary_key=True), "price": IntField()})
    await round_trip.migrate_to(model)
    result = await detect_drift(round_trip.connection, build_live_state(model), [APP_LABEL])
    assert (result.operations, result.mismatched_columns) == ([], [])

    (primary_key_row,) = await round_trip.connection.execute_dicts(
        "SELECT conname FROM pg_constraint WHERE conrelid = 'drift_keyed'::regclass AND contype = 'p'"
    )
    await round_trip.connection.execute_script(
        f'ALTER TABLE drift_keyed DROP CONSTRAINT "{primary_key_row["conname"]}"'
    )
    result = await detect_drift(round_trip.connection, build_live_state(model), [APP_LABEL])
    assert [(mismatch.column, mismatch.detail) for mismatch in result.mismatched_columns] == [
        ("id", "is not in the table's PRIMARY KEY, the model's primary key holds it")
    ]


@pytest.mark.asyncio
async def test_detect_drift_reports_a_changed_predicate_or_method_of_an_exclusion_constraint():
    """The declared condition was taken whatever the database's was - a predicate added by hand was
    no drift; and an EXCLUDE USING hash wasn't read, so drift added the constraint without removing it."""
    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    if DatabaseUnderTest.get_dialect().name != "postgresql":
        pytest.skip("ExclusionConstraint/EXCLUDE is Postgres-only")
    async with hare_test_context(
        modules=["tests.migrations.drift.test_drift"],
        db_url=db_url,
        app_label="drift_models_exclusion",
        connection_label="models",
    ) as ctx:
        connection = Connections.get("models")
        apps_config = {"drift_models_exclusion": {"models": [], "default_connection": "models"}}
        new_state = MigrationAutodetector(ctx.apps, apps_config).current_state()

        async def get_operation_types() -> list[str]:
            result = await detect_drift(
                connection, new_state, ["drift_models_exclusion"], schema=DatabaseUnderTest.get_public_schema()
            )
            return sorted(type(operation).__name__ for operation in result.operations)

        for definition in (
            "EXCLUDE USING btree (resource WITH =) WHERE (NOT cancelled AND resource > 1)",
            "EXCLUDE USING btree (resource WITH =)",
            "EXCLUDE USING hash (resource WITH =) WHERE (NOT cancelled)",
        ):
            await connection.execute_script(
                "ALTER TABLE drift_exclusion_widget DROP CONSTRAINT drift_excl_no_overlap; "
                f"ALTER TABLE drift_exclusion_widget ADD CONSTRAINT drift_excl_no_overlap {definition}"
            )
            assert await get_operation_types() == ["AddConstraint", "RemoveConstraint"], definition


@pytest.mark.asyncio
@pytest.mark.parametrize("column_type", ["text", "varchar(20)"])
async def test_detect_drift_reports_a_native_enum_column_turned_into_text(db_isolated_no_schema, column_type):
    """A type of its own was outside the types compared, and a varchar column read as a CharField
    resized - an enum column turned into text was no drift; and the type no column was of any more
    was created again."""
    import enum

    from hare.dialects.postgresql.fields import NativeEnumField
    from hare.fields import IntField
    from hare.migrations.operations import AlterField
    from tests.migrations.round_trips.test_round_trip_real_db import (
        APP_LABEL,
        RoundTrip,
        build_live_state,
        build_model,
    )

    class DriftMood(enum.Enum):
        SAD = "sad"
        OK = "ok"

    round_trip = RoundTrip(db_isolated_no_schema.get_connection())
    if round_trip.dialect != "postgresql":
        pytest.skip("ENUM types are PostgreSQL's")
    model = build_model(
        "Moody",
        "drift_moody",
        {
            "id": IntField(primary_key=True, generated=False),
            "mood": NativeEnumField(DriftMood, type_name="drift_mood"),
        },
    )
    await round_trip.migrate_to(model)
    await round_trip.connection.execute_script(
        f"ALTER TABLE drift_moody ALTER COLUMN mood TYPE {column_type} USING mood::text"
    )
    result = await detect_drift(round_trip.connection, build_live_state(model), [APP_LABEL])
    assert [type(operation) for operation in result.operations] == [AlterField]


@pytest.mark.asyncio
async def test_detect_drift_reports_a_dropped_foreign_key_of_a_through_table():
    """A many-to-many relation's through table was checked for its columns, its UNIQUE and its
    indexes, never its FOREIGN KEY constraints."""
    from hare.migrations.operations import AlterField

    if DatabaseUnderTest.get_dialect().name != "postgresql":
        pytest.skip("SQLite drops a FOREIGN KEY only by rebuilding the table")
    async with hare_test_context(
        modules=["tests.migrations.drift.test_drift"],
        db_url=os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:"),
        app_label="drift_models",
        connection_label="models",
    ) as ctx:
        connection = Connections.get("models")

        async def get_altered_field_names() -> list[str]:
            result = await detect_drift(
                connection, current_state(ctx), ["drift_models"], schema=DatabaseUnderTest.get_public_schema()
            )
            return [operation.name for operation in result.operations if isinstance(operation, AlterField)]

        assert await get_altered_field_names() == []
        rows = await connection.execute_dicts(
            "SELECT conname FROM pg_constraint WHERE conrelid = 'drift_m2m_post_label'::regclass AND contype = 'f' "
            "ORDER BY conname"
        )
        await connection.execute_script(f'ALTER TABLE drift_m2m_post_label DROP CONSTRAINT "{rows[0]["conname"]}"')
        assert await get_altered_field_names() == ["labels"]


@pytest.mark.asyncio
async def test_detect_drift_reports_an_extension_type_column_turned_into_text(db_isolated_no_schema):
    """A type outside the types compared - citext, hstore, ltree - was never reported, though the
    column now held a built-in one."""
    from hare.dialects.postgresql.fields import CitextField
    from hare.fields import IntField
    from hare.migrations.operations import AlterField
    from tests.migrations.round_trips.test_round_trip_real_db import (
        APP_LABEL,
        RoundTrip,
        build_live_state,
        build_model,
    )

    round_trip = RoundTrip(db_isolated_no_schema.get_connection())
    if round_trip.dialect != "postgresql":
        pytest.skip("citext is PostgreSQL's")
    model = build_model(
        "Mailbox", "drift_mailbox", {"id": IntField(primary_key=True, generated=False), "mail": CitextField()}
    )
    await round_trip.migrate_to(model)
    result = await detect_drift(round_trip.connection, build_live_state(model), [APP_LABEL])
    assert result.operations == []
    await round_trip.connection.execute_script("ALTER TABLE drift_mailbox ALTER COLUMN mail TYPE text")
    result = await detect_drift(round_trip.connection, build_live_state(model), [APP_LABEL])
    assert [type(operation) for operation in result.operations] == [AlterField]


@pytest.mark.asyncio
async def test_detect_drift_reads_a_triggers_condition_and_whether_it_fires(db_isolated_no_schema):
    """A trigger's WHEN condition was taken as declared whatever the database's was, and a disabled
    trigger was the same as the declared one; a trigger declared disabled is created so."""
    from hare.ddl import Trigger
    from hare.fields import IntField
    from hare.migrations.operations import AlterTrigger
    from tests.migrations.round_trips.test_round_trip_real_db import (
        APP_LABEL,
        RoundTrip,
        build_live_state,
        build_model,
    )

    round_trip = RoundTrip(db_isolated_no_schema.get_connection())
    if round_trip.dialect != "postgresql":
        pytest.skip("SQLite fires every trigger it has")

    def get_model(enabled: bool) -> type:
        return build_model(
            "Priced",
            "drift_priced",
            {"id": IntField(primary_key=True, generated=False), "price": IntField()},
            {
                "triggers": [
                    Trigger(
                        name="drift_priced_when",
                        on="UPDATE",
                        body=RawSQLTerm("RETURN NEW;"),
                        timing=TriggerTiming.BEFORE,
                        when=RawSQLTerm("NEW.price > 1"),
                        enabled=enabled,
                    )
                ]
            },
        )

    async def get_operation_types(model: type) -> list[type]:
        result = await detect_drift(round_trip.connection, build_live_state(model), [APP_LABEL])
        return [type(operation) for operation in result.operations]

    enabled_model = get_model(True)
    await round_trip.migrate_to(enabled_model)
    assert await get_operation_types(enabled_model) == []
    await round_trip.connection.execute_script("ALTER TABLE drift_priced DISABLE TRIGGER drift_priced_when")
    assert await get_operation_types(enabled_model) == [AlterTrigger]
    await round_trip.connection.execute_script(
        "ALTER TABLE drift_priced ENABLE TRIGGER drift_priced_when; DROP TRIGGER drift_priced_when ON drift_priced; "
        "CREATE TRIGGER drift_priced_when BEFORE UPDATE ON drift_priced FOR EACH ROW WHEN (NEW.price > 2) "
        "EXECUTE FUNCTION drift_priced_when_fn()"
    )
    assert await get_operation_types(enabled_model) == [AlterTrigger]

    disabled_model = get_model(False)
    await round_trip.migrate_to(disabled_model)
    assert await get_operation_types(disabled_model) == []
    [row] = await round_trip.connection.execute_dicts(
        "SELECT tgenabled::text AS state FROM pg_trigger WHERE tgname = 'drift_priced_when'"
    )
    assert row["state"] == "D"


@pytest.mark.database_independent
def test_a_disabled_trigger_is_refused_on_sqlite():
    from hare.ddl import Trigger
    from hare.dialects.sqlite.constants import SQLITE_DIALECT
    from hare.exceptions import UnSupportedError
    from hare.fields import IntField
    from tests.migrations.round_trips.test_round_trip_real_db import build_model
    from tests.utils.fake_client import FakeClient

    model = build_model("Muted", "drift_muted", {"id": IntField(primary_key=True)})
    editor = SQLITE_DIALECT.schema_editor_class(FakeClient("sqlite"))
    trigger = Trigger(name="drift_muted_insert", on="INSERT", body=RawSQLTerm("SELECT 1;"), enabled=False)
    with pytest.raises(UnSupportedError, match="disabled trigger"):
        editor.trigger_statements.get_trigger_create_sqls(model, trigger)
