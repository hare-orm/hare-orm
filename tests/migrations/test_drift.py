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
from hare.dialects.postgresql.postgresql_introspector import PostgresqlIntrospector
from hare.dialects.sqlite.sqlite_introspector import SqliteIntrospector
from hare.inspectdb.introspection.database_catalog import DatabaseCatalog
from hare.inspectdb.introspection.index_info import IndexInfo
from hare.inspectdb.introspection.table_info import TableInfo
from hare.migrations.autodetection.migration_autodetector import MigrationAutodetector
from hare.migrations.drift import DriftResult, detect_drift
from hare.migrations.drift.observed.observed_indexes import ObservedIndexes
from hare.migrations.drift.observed.observed_meta_options import ObservedMetaOptions
from hare.migrations.drift.observed.observed_uniqueness import ObservedUniqueness
from hare.migrations.operations import AddConstraint, AddField, AddIndex
from hare.migrations.state.model_state import ModelState
from hare.models import Model
from hare.query.expressions import Q
from hare.query.functions import Lower
from hare.vectors import VectorField
from tests.migrations.test_round_trip_real_db import APP_LABEL, RoundTrip, build_live_state, build_model
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


@pytest.mark.asyncio
async def test_detect_drift_reports_no_drift_on_a_synced_schema():
    async with hare_test_context(
        modules=["tests.migrations.test_drift"], app_label="drift_models", connection_label="models"
    ) as ctx:
        connection = Connections.get("models")
        result = await detect_drift(connection, current_state(ctx), ["drift_models"], schema="public")

        assert isinstance(result, DriftResult)
        assert result.has_drift is False
        assert result.operations == []
        assert result.untracked_tables == []
        assert result.untracked_columns == []


@pytest.mark.asyncio
async def test_detect_drift_reports_no_drift_for_a_synced_db_index_field():
    """DriftStateBuilder used to synthesize the implicit per-field Index the live model's own
    ModelState.make_from_model() adds for a plain db_index=True field only on the "new_state"
    (live models) side, never on the observed database side - a freshly-applied db_index=True
    field showed permanent, unfixable "Add index" drift even though the database already has
    exactly the index the model declares. Likewise a plain CheckConstraint of Meta.constraints,
    which the observed side never read, showed as a phantom AddConstraint."""
    async with hare_test_context(
        modules=["tests.migrations.test_drift"], app_label="drift_models", connection_label="models"
    ) as ctx:
        connection = Connections.get("models")
        result = await detect_drift(connection, current_state(ctx), ["drift_models"], schema="public")

        assert result.has_drift is False
        assert result.operations == []


@pytest.mark.asyncio
async def test_detect_drift_reports_no_drift_for_a_foreign_key_with_a_source_field_override():
    """DriftStateBuilder._expected_columns()/_column_to_field_name() used to read a relational
    field's source_fields (fields_map keys, e.g. "target_id") as if it were the real database
    column name - for a source_field= override (real column "target_ref") this diverges, so the
    real column was reported as untracked even though the model correctly declares it."""
    async with hare_test_context(
        modules=["tests.migrations.test_drift"], app_label="drift_models", connection_label="models"
    ) as ctx:
        connection = Connections.get("models")
        result = await detect_drift(connection, current_state(ctx), ["drift_models"], schema="public")

        assert result.has_drift is False
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
        modules=["tests.migrations.test_drift"],
        db_url=db_url,
        app_label="drift_models_custom_schema",
        connection_label="models",
    ) as ctx:
        connection = Connections.get("models")
        apps_config = {"drift_models_custom_schema": {"models": [], "default_connection": "models"}}
        new_state = MigrationAutodetector(ctx.apps, apps_config).current_state()
        result = await detect_drift(connection, new_state, ["drift_models_custom_schema"], schema="public")

        assert result.has_drift is False
        assert result.operations == []
        assert result.untracked_tables == []
        assert result.untracked_columns == []


@pytest.mark.asyncio
async def test_detect_drift_reports_untracked_table():
    """A table created via raw SQL - bypassing hare's own model/migration bookkeeping entirely -
    must be reported, without going through the CLI/argparse layer at all."""
    async with hare_test_context(
        modules=["tests.migrations.test_drift"], app_label="drift_models", connection_label="models"
    ) as ctx:
        connection = Connections.get("models")
        await connection.execute_script('CREATE TABLE "untracked_thing" (id INTEGER PRIMARY KEY)')

        result = await detect_drift(connection, current_state(ctx), ["drift_models"], schema="public")

        assert result.has_drift is True
        assert result.untracked_tables == ["untracked_thing"]
        assert result.operations == []
        assert result.untracked_columns == []


@pytest.mark.asyncio
async def test_detect_drift_reports_untracked_column():
    """A column added via raw SQL on an otherwise-tracked table must be reported as untracked,
    distinctly from an untracked table."""
    async with hare_test_context(
        modules=["tests.migrations.test_drift"], app_label="drift_models", connection_label="models"
    ) as ctx:
        connection = Connections.get("models")
        await connection.execute_script('ALTER TABLE "drift_widget" ADD COLUMN extra_column TEXT')

        result = await detect_drift(connection, current_state(ctx), ["drift_models"], schema="public")

        assert result.has_drift is True
        assert result.untracked_columns == [("drift_models", "DriftWidget", "drift_widget", "extra_column")]
        assert result.untracked_tables == []
        assert result.operations == []


@pytest.mark.asyncio
async def test_detect_drift_reports_missing_field_as_add_field_operation():
    """A model field with no matching database column shows up as a real AddField Operation -
    the same operation makemigrations would write - not just a plain-text report."""
    async with hare_test_context(
        modules=["tests.migrations.test_drift"], app_label="drift_models", connection_label="models"
    ) as ctx:
        connection = Connections.get("models")
        new_state = current_state(ctx)
        # Add a field to the in-memory State only (not the real, already-created table) - the
        # same shape of drift a model change that was never migrated would produce.
        new_state.models[("drift_models", "DriftWidget")].fields["description"] = fields.CharField(
            max_length=100, null=True
        )

        result = await detect_drift(connection, new_state, ["drift_models"], schema="public")

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
        modules=["tests.migrations.test_drift"], app_label="drift_models", connection_label="models"
    ) as ctx:
        connection = Connections.get("models")
        await _drop_index_on_columns(connection, "drift_indexed_widget", ["f1", "f2"], unique=False)

        result = await detect_drift(connection, current_state(ctx), ["drift_models"], schema="public")

        assert result.has_drift is True
        assert any(isinstance(operation, AddIndex) for operation in result.operations)
        assert result.untracked_tables == []
        assert result.untracked_columns == []


def test_build_observed_indexes_preserves_unique_on_a_special_index():
    """_build_observed_indexes used to drop `unique` entirely when reconstructing a "special"
    (condition/expression/opclass) index - the observed state (what's actually in the DB) then
    permanently disagreed with the declared state (unique=True) on every detect_drift() run, even
    right after a clean migrate with zero real divergence, since _index_signature() (correctly)
    includes `unique` in its comparison."""
    table_info = TableInfo(
        name="widget",
        indexes=[IndexInfo(columns=["email"], is_unique=True, condition_sql="active = true")],
    )

    (index,) = ObservedIndexes.build_observed_indexes(PostgresqlIntrospector, table_info, {})

    assert isinstance(index, PartialIndex)
    assert index.unique is True


def test_build_observed_indexes_drops_unique_only_for_a_non_btree_access_method():
    """Postgres itself rejects UNIQUE on a non-btree access method (GIN here) - there's no
    faithful way to carry it into the observed state, so this stays the one case where dropping
    it (instead of a permanently wrong `unique=True` no live index could ever satisfy) is
    correct."""
    table_info = TableInfo(
        name="widget",
        indexes=[IndexInfo(columns=["tags"], is_unique=True, index_type="gin")],
    )

    (index,) = ObservedIndexes.build_observed_indexes(PostgresqlIntrospector, table_info, {})

    assert index.unique is False


def test_build_observed_indexes_recognizes_hnsw_and_ivfflat_index_types():
    """ "hnsw"/"ivfflat" (pgvector) were missing from INDEX_TYPE_CLASSES entirely - falling
    through to the plain btree Index/PartialIndex default, which is worse than just "loses
    tuning": a `vector` column has no default btree operator class at all, so the observed side
    could never even match a correctly-declared HnswIndex/IvfflatIndex, reporting permanent,
    unfixable drift."""
    from hare.dialects.postgresql.indexes import HnswIndex, IvfflatIndex

    table_info = TableInfo(
        name="item",
        indexes=[
            IndexInfo(columns=["embedding"], is_unique=False, index_type="hnsw", opclasses=["vector_l2_ops"]),
            IndexInfo(columns=["embedding2"], is_unique=False, index_type="ivfflat"),
        ],
    )

    hnsw_index, ivfflat_index = ObservedIndexes.build_observed_indexes(PostgresqlIntrospector, table_info, {})

    assert isinstance(hnsw_index, HnswIndex)
    assert hnsw_index.m == HnswIndex.DEFAULT_M
    assert hnsw_index.ef_construction == HnswIndex.DEFAULT_EF_CONSTRUCTION
    assert isinstance(ivfflat_index, IvfflatIndex)
    assert ivfflat_index.lists == IvfflatIndex.DEFAULT_LISTS


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


@pytest.mark.asyncio
async def test_detect_drift_reports_dropped_unique_constraint():
    """A UniqueConstraint dropped directly against the database must be reported as drift - same
    regression as test_detect_drift_reports_dropped_multi_column_index, covering the unique
    constraint reconstruction path instead of Meta.indexes."""
    async with hare_test_context(
        modules=["tests.migrations.test_drift"], app_label="drift_models", connection_label="models"
    ) as ctx:
        connection = Connections.get("models")
        await _drop_unique_constraint(connection, "drift_indexed_widget")

        result = await detect_drift(connection, current_state(ctx), ["drift_models"], schema="public")

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
        modules=["tests.migrations.test_drift"],
        db_url=db_url,
        app_label="drift_models_expression_index",
        connection_label="models",
    ) as ctx:
        connection = Connections.get("models")
        apps_config = {"drift_models_expression_index": {"models": [], "default_connection": "models"}}
        new_state = MigrationAutodetector(ctx.apps, apps_config).current_state()
        result = await detect_drift(connection, new_state, ["drift_models_expression_index"], schema="public")

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
    if not DatabaseUnderTest.get_dialect().features.supports_exclusion_constraints:
        pytest.skip("ExclusionConstraint/EXCLUDE is Postgres-only")
    async with hare_test_context(
        modules=["tests.migrations.test_drift"],
        db_url=db_url,
        app_label="drift_models_exclusion",
        connection_label="models",
    ) as ctx:
        connection = Connections.get("models")
        apps_config = {"drift_models_exclusion": {"models": [], "default_connection": "models"}}
        new_state = MigrationAutodetector(ctx.apps, apps_config).current_state()
        result = await detect_drift(connection, new_state, ["drift_models_exclusion"], schema="public")

        assert result.has_drift is False
        assert result.operations == []


@pytest.mark.asyncio
async def test_detect_drift_reports_no_drift_for_synced_partial_indexes():
    """A partial index whose predicate came back as '"isbn" = \'x\'' (SQLite) or
    "((isbn)::text = 'x'::text)" (Postgres) was left unparsed and rebuilt without its condition,
    reporting a Remove/Add index pair on every run."""
    async with hare_test_context(
        modules=["tests.migrations.test_drift"], app_label="drift_models", connection_label="models"
    ) as ctx:
        connection = Connections.get("models")
        result = await detect_drift(connection, current_state(ctx), ["drift_models"], schema="public")

        assert result.operations == []


@pytest.mark.parametrize(
    ("predicate_sql", "expected_condition"),
    [
        ("\"isbn\" = 'x'", {"isbn": "x"}),
        ("((isbn)::text = 'x'::text)", {"isbn": "x"}),
        ("(((isbn)::text = 'x'::text) AND (copies = 1))", {"isbn": "x", "copies": 1}),
        ('("we""ird" = 2)', {'we"ird': 2}),
        ("copies IS NOT NULL", None),
    ],
)
def test_get_predicate_equalities_accepts_quoted_and_cast_columns(predicate_sql, expected_condition):
    from hare.dialects.postgresql.postgresql_introspector import PostgresqlIntrospector

    assert PostgresqlIntrospector.get_predicate_equalities(predicate_sql) == expected_condition


def test_build_observed_indexes_trusts_a_same_named_declared_partial_index_for_an_unparsed_condition():
    declared_index = PartialIndex(fields=("isbn",), condition=RawSQLTerm("copies IS NOT NULL"), name="raw_partial")
    table_info = TableInfo(
        name="widget",
        columns=[],
        indexes=[IndexInfo(name="raw_partial", columns=["isbn"], is_unique=False, condition_sql="copies IS NOT NULL")],
    )

    (observed_index,) = ObservedIndexes.build_observed_indexes(
        PostgresqlIntrospector, table_info, {}, [declared_index]
    )
    (unmatched_index,) = ObservedIndexes.build_observed_indexes(PostgresqlIntrospector, table_info, {}, [])

    assert observed_index.condition is declared_index.condition
    assert unmatched_index.condition == RawSQLTerm("copies IS NOT NULL")


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
        modules=["tests.migrations.test_drift"], app_label="drift_models", connection_label="models"
    ) as ctx:
        connection = Connections.get("models")
        await connection.execute_script("DROP TABLE drift_m2m_post_label")
        if through_table_ddl is not None:
            await connection.execute_script(through_table_ddl)

        result = await detect_drift(connection, current_state(ctx), ["drift_models"], schema="public")

        assert result.has_drift is True
        assert [
            (type(operation), operation.model_name, operation.name)
            for operation in result.operations
            if isinstance(operation, AddField)
        ] == [(AddField, "DriftManyToManyPost", "labels")]
        assert result.untracked_tables == []


def test_build_observed_indexes_carries_the_database_name_onto_an_expression_index():
    """An expression-based index has no column list, so its real database name is the only thing
    a RemoveIndex for it (when the models don't declare it) can identify it by - the observed
    Index used to be built nameless, which made that RemoveIndex impossible to construct."""
    table_info = TableInfo(
        name="widget",
        indexes=[
            IndexInfo(
                columns=[],
                is_unique=True,
                expression_terms=['"group"', "COALESCE(variant, ''::character varying)"],
                name="widget_group_variant_key",
            )
        ],
    )

    (index,) = ObservedIndexes.build_observed_indexes(PostgresqlIntrospector, table_info, {})

    assert index.name == "widget_group_variant_key"


@pytest.mark.asyncio
async def test_build_database_state_inspects_a_schemas_tables_in_one_batch(monkeypatch):
    """build_database_state() reads every model table of a schema with one inspect_tables() call -
    a fixed number of catalog queries on Postgres, not several per table."""
    async with hare_test_context(
        modules=["tests.migrations.test_drift"], app_label="drift_models", connection_label="models"
    ) as ctx:
        connection = Connections.get("models")
        inspected_table_lists: list[list[str]] = []
        original_inspect_tables = DatabaseCatalog.inspect_tables

        async def recording_inspect_tables(connection, tables, schema="public", *, verify_exists=True):
            inspected_table_lists.append(list(tables))
            return await original_inspect_tables(connection, tables, schema=schema, verify_exists=verify_exists)

        monkeypatch.setattr(DatabaseCatalog, "inspect_tables", recording_inspect_tables)

        await detect_drift(connection, current_state(ctx), ["drift_models"], schema="public")

        batched_table_lists = [tables for tables in inspected_table_lists if len(tables) > 1]
        assert len(batched_table_lists) == 1, "expected the schema's tables to be inspected in one batch"


@pytest.mark.asyncio
async def test_build_database_state_skips_inspect_tables_own_redundant_existence_check(monkeypatch):
    """Every table build_database_state() inspects was already matched against a table list it
    just fetched - inspect_tables()'s own get_table_names() re-check would be a wasted round trip,
    so it must be called with verify_exists=False."""
    async with hare_test_context(
        modules=["tests.migrations.test_drift"], app_label="drift_models", connection_label="models"
    ) as ctx:
        connection = Connections.get("models")
        verify_exists_seen: list[bool] = []
        original_inspect_tables = DatabaseCatalog.inspect_tables

        async def recording_inspect_tables(connection, tables, schema="public", *, verify_exists=True):
            verify_exists_seen.append(verify_exists)
            return await original_inspect_tables(connection, tables, schema=schema, verify_exists=verify_exists)

        monkeypatch.setattr(DatabaseCatalog, "inspect_tables", recording_inspect_tables)

        await detect_drift(connection, current_state(ctx), ["drift_models"], schema="public")

        assert verify_exists_seen, "expected at least one table to be inspected"
        assert all(seen is False for seen in verify_exists_seen)


@pytest.mark.asyncio
async def test_detect_drift_reports_no_drift_for_a_json_column_created_with_the_older_sqlite_type():
    """JSONField's SQLite column type changed from JSON to JSON_TEXT - a table created with the
    older type must not show up as drift."""
    async with hare_test_context(
        modules=["tests.migrations.test_drift"], app_label="drift_models", connection_label="models"
    ) as ctx:
        connection = Connections.get("models")
        if connection.dialect.name != "sqlite":
            pytest.skip("SQLite column type only")
        await connection.execute_script(
            'DROP TABLE "drift_json_document"; '
            'CREATE TABLE "drift_json_document" ("id" INT NOT NULL PRIMARY KEY, "payload" JSON)'
        )

        result = await detect_drift(connection, current_state(ctx), ["drift_models"], schema="public")

        assert result.has_drift is False
        assert result.operations == []


def _postgres_test_database_url_or_skip() -> str:
    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    if DatabaseUnderTest.get_dialect().name != "postgresql":
        pytest.skip("Postgres-only drift scenario")
    return db_url


@pytest.mark.asyncio
async def test_detect_drift_checks_a_many_to_many_through_table_in_its_owner_schema():
    """The through-table check reused the table list of the last model the first pass visited -
    here the tag's other schema - so the post's through table looked missing and drift reported
    a phantom AddField for the relation."""
    db_url = _postgres_test_database_url_or_skip()
    async with hare_test_context(
        modules=["tests.migrations.test_drift"],
        db_url=db_url,
        app_label="drift_models_schema_m2m",
        connection_label="models",
    ) as ctx:
        connection = Connections.get("models")
        apps_config = {"drift_models_schema_m2m": {"models": [], "default_connection": "models"}}
        new_state = MigrationAutodetector(ctx.apps, apps_config).current_state()
        result = await detect_drift(connection, new_state, ["drift_models_schema_m2m"], schema="public")

        assert result.operations == []


@pytest.mark.asyncio
async def test_detect_drift_trusts_the_declared_raw_expression_of_an_exclusion_constraint():
    """A raw SQL EXCLUDE expression came back rewritten by Postgres and never equalled the declared
    RawSQLTerm - a Remove/Add constraint pair on every run."""
    db_url = _postgres_test_database_url_or_skip()
    async with hare_test_context(
        modules=["tests.migrations.test_drift"],
        db_url=db_url,
        app_label="drift_models_exclusion_raw",
        connection_label="models",
    ) as ctx:
        connection = Connections.get("models")
        apps_config = {"drift_models_exclusion_raw": {"models": [], "default_connection": "models"}}
        new_state = MigrationAutodetector(ctx.apps, apps_config).current_state()
        result = await detect_drift(connection, new_state, ["drift_models_exclusion_raw"], schema="public")

        assert result.operations == []


@pytest.mark.asyncio
async def test_detect_drift_reads_vector_index_storage_parameters():
    """The introspector never read an index's WITH (...) parameters, so an IvfflatIndex/HnswIndex
    with non-default lists/m/ef_construction was rebuilt with the class defaults - and a declared
    default opclass (reported by the database as no opclass) never matched either."""
    db_url = _postgres_test_database_url_or_skip()
    try:
        context_manager = hare_test_context(
            modules=["tests.migrations.test_drift"],
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
        result = await detect_drift(connection, new_state, ["drift_models_vector"], schema="public")

        assert result.operations == []
    finally:
        await context_manager.__aexit__(None, None, None)


def test_build_observed_indexes_passes_storage_parameters_to_vector_indexes():
    table_info = TableInfo(
        name="item",
        indexes=[
            IndexInfo(
                columns=["embedding"],
                is_unique=False,
                index_type="hnsw",
                opclasses=["vector_cosine_ops"],
                storage_parameters={"m": "8", "ef_construction": "32"},
            ),
            IndexInfo(
                columns=["embedding2"], is_unique=False, index_type="ivfflat", storage_parameters={"lists": "5"}
            ),
        ],
    )

    hnsw_index, ivfflat_index = ObservedIndexes.build_observed_indexes(PostgresqlIntrospector, table_info, {})

    assert (hnsw_index.m, hnsw_index.ef_construction) == (8, 32)
    assert ivfflat_index.lists == 5


def test_build_observed_indexes_keeps_a_declared_default_opclass_of_a_same_named_index():
    declared_index = IvfflatIndex(fields=("embedding",), lists=5, opclasses=("vector_l2_ops",))
    generated_name = ObservedIndexes.get_declared_indexes_by_name("item", {}, [declared_index])
    (index_name,) = generated_name
    table_info = TableInfo(
        name="item",
        indexes=[
            IndexInfo(
                columns=["embedding"],
                is_unique=False,
                name=index_name,
                index_type="ivfflat",
                default_opclasses=["vector_l2_ops"],
                storage_parameters={"lists": "5"},
            )
        ],
    )

    (observed_index,) = ObservedIndexes.build_observed_indexes(
        PostgresqlIntrospector, table_info, {}, [declared_index]
    )
    (undeclared_index,) = ObservedIndexes.build_observed_indexes(PostgresqlIntrospector, table_info, {}, [])

    assert tuple(observed_index.opclasses) == ("vector_l2_ops",)
    assert not undeclared_index.opclasses


def test_build_observed_indexes_keeps_an_undeclared_partial_index_condition_as_the_database_has_it():
    """With no declared index to compare with, the predicate is the database's own text, over the
    real column names."""
    table_info = TableInfo(
        name="coupon",
        indexes=[IndexInfo(columns=["code_col"], is_unique=False, name="coupon_code", condition_sql="code_col = 'x'")],
    )

    (observed_index,) = ObservedIndexes.build_observed_indexes(
        PostgresqlIntrospector, table_info, {"code_col": "code"}
    )

    assert tuple(observed_index.fields) == ("code",)
    assert observed_index.condition == RawSQLTerm("code_col = 'x'")


def test_build_observed_indexes_matches_an_equality_string_condition_of_an_unnamed_index():
    declared_index = PartialIndex(fields=("copies",), condition=RawSQLTerm("copies = 2"))
    (index_name,) = ObservedIndexes.get_declared_indexes_by_name("coupon", {}, [declared_index])
    table_info = TableInfo(
        name="coupon",
        indexes=[IndexInfo(columns=["copies"], is_unique=False, name=index_name, condition_sql="(copies = 2)")],
    )

    (observed_index,) = ObservedIndexes.build_observed_indexes(
        PostgresqlIntrospector, table_info, {}, [declared_index]
    )

    assert observed_index.condition is declared_index.condition


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
async def test_detect_drift_matches_unique_constraints_and_unique_indexes_to_their_declarations(db_isolated):
    """A single-column UniqueConstraint and every Index(unique=True) came back as a unique=True field
    or a unique_together entry, and an unnamed expression index was never matched by its generated
    name - a false Alter field/Add constraint/Add index on a schema fresh out of `migrate`."""
    round_trip = RoundTrip(db_isolated.get_connection())
    widget = _build_unique_drift_widget()
    await round_trip.migrate_to(widget)

    result = await detect_drift(round_trip.connection, build_live_state(widget), [APP_LABEL])

    assert result.operations == []


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_detect_drift_still_reports_a_dropped_single_column_unique_constraint(db_isolated):
    round_trip = RoundTrip(db_isolated.get_connection())
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
async def test_detect_drift_reports_a_renamed_unique_constraint_as_a_rename(db_isolated):
    round_trip = RoundTrip(db_isolated.get_connection())
    await round_trip.migrate_to(_build_unique_drift_widget())
    renamed_widget = _build_unique_drift_widget(
        constraints=[
            UniqueConstraint(fields=("code",), name="uq_udw_code_renamed"),
            UniqueConstraint(fields=("a", "b"), name="uq_udw_ab"),
            UniqueConstraint(fields=("c",)),
        ]
    )

    result = await detect_drift(round_trip.connection, build_live_state(renamed_widget), [APP_LABEL])

    assert [(type(operation).__name__, operation.old_name, operation.new_name) for operation in result.operations] == [
        ("RenameConstraint", "uq_udw_code", "uq_udw_code_renamed")
    ]


@pytest.mark.asyncio
async def test_detect_drift_matches_an_inlined_sqlite_unique_constraint_by_its_declared_name(db_isolated):
    """A table with a CheckConstraint gets its UniqueConstraints inlined into CREATE TABLE, backed
    by sqlite_autoindex_* indexes whose names never equalled the declared ones - a false
    Remove/Add pair for every one of them."""
    round_trip = RoundTrip(db_isolated.get_connection())
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
async def test_detect_drift_matches_conditional_and_deferrable_unique_constraints(db_isolated):
    """A conditional UniqueConstraint came back as a PartialIndex (a Remove index/Add constraint
    pair), and a constraint's DEFERRABLE flags were never read, so a changed one went unseen."""
    round_trip = RoundTrip(db_isolated.get_connection())
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
async def test_detect_drift_reports_a_changed_predicate_of_a_conditional_unique_constraint(db_isolated):
    round_trip = RoundTrip(db_isolated.get_connection())
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

    assert sorted(type(operation).__name__ for operation in result.operations) == ["AddConstraint", "RemoveIndex"]


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
async def test_detect_drift_uses_the_declared_wording_of_a_reserialized_trigger(db_isolated):
    """The introspector uppercased the UPDATE OF column, Postgres rewrites a WHEN condition as
    "(new.n > 5)", and the body comes back without its surrounding whitespace - a permanent Alter
    trigger for a trigger nobody touched."""
    round_trip = RoundTrip(db_isolated.get_connection())
    widget = _build_trigger_drift_widget(round_trip.dialect)
    await round_trip.migrate_to(widget)

    result = await detect_drift(round_trip.connection, build_live_state(widget), [APP_LABEL])

    assert result.operations == []


@pytest.mark.asyncio
async def test_detect_drift_still_reports_a_changed_trigger_body(db_isolated):
    round_trip = RoundTrip(db_isolated.get_connection())
    await round_trip.migrate_to(_build_trigger_drift_widget(round_trip.dialect))
    changed_body = (
        "UPDATE trigger_drift_widget SET m = 2 WHERE id = NEW.id;"
        if round_trip.dialect == "sqlite"
        else "RETURN NULL;"
    )
    changed_widget = _build_trigger_drift_widget(round_trip.dialect, changed_body)

    result = await detect_drift(round_trip.connection, build_live_state(changed_widget), [APP_LABEL])

    assert [type(operation).__name__ for operation in result.operations] == ["AlterTrigger"]


def test_observed_trigger_keeps_its_own_wording_when_the_events_differ():
    declared = Trigger(name="trg", on="UPDATE OF n", when=RawSQLTerm("NEW.n > 5"), body=RawSQLTerm("RETURN NEW;"))
    observed = Trigger(name="trg", on="UPDATE OF m", when=RawSQLTerm("(new.n > 5)"), body=RawSQLTerm("RETURN NEW;"))

    assert ObservedMetaOptions.get_observed_trigger(observed, declared) == observed


def test_observed_trigger_reports_a_when_condition_present_on_one_side_only():
    declared = Trigger(name="trg", on="UPDATE", body=RawSQLTerm("RETURN NEW;"))
    observed = Trigger(name="trg", on="UPDATE", when=RawSQLTerm("(new.n > 5)"), body=RawSQLTerm("RETURN NEW;"))

    assert ObservedMetaOptions.get_observed_trigger(observed, declared).when == RawSQLTerm("(new.n > 5)")


def test_match_declared_unique_indexes_reconstructs_a_folded_single_column_unique_constraint():
    model_state = ModelState.make_from_model(APP_LABEL, _build_unique_drift_widget())
    code_index = IndexInfo(columns=["code"], is_unique=True, name="uq_udw_code", deferrable=True)
    field_index = IndexInfo(columns=["e"], is_unique=True, name="unique_drift_widget_e_key")
    table_info = TableInfo(name="unique_drift_widget", column_indexes=[code_index, field_index])

    matches = ObservedUniqueness.match_declared_unique_indexes(SqliteIntrospector, model_state, table_info)

    assert matches == [(code_index, UniqueConstraint(fields=("code",), name="uq_udw_code", deferrable=True))]
