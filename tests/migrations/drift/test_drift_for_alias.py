"""Tests for detect_drift_for_alias(), the one-call drift check for a single connection alias."""

from __future__ import annotations

import datetime

import pytest

from hare import Hare, fields
from hare.contrib.test.databases.temporary_databases import TemporaryDatabases
from hare.contrib.test.isolated_contexts import hare_test_context
from hare.core.hare_context import HareContext
from hare.exceptions import ConfigurationError
from hare.migrations.drift import DriftResult, detect_drift, detect_drift_for_alias
from hare.migrations.state.migration_environment import MigrationEnvironment
from hare.migrations.state.state import State
from hare.models import Model
from tests.utils.database_under_test import DatabaseUnderTest


class AliasDriftWidget(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
    # A UNIQUE column's backing index must not read back as a separate db_index=True index.
    code = fields.CharField(max_length=10, unique=True, null=True)
    slug = fields.CharField(max_length=10, unique=True, db_index=True, null=True)

    class Meta:
        app = "alias_drift"
        table = "alias_drift_widget"


class AliasDriftExternal(Model):
    """Wraps a table hare-orm never creates/alters/drops itself."""

    id = fields.IntField(primary_key=True)
    title = fields.TextField()

    class Meta:
        app = "alias_drift"
        table = "alias_drift_external"
        managed = False


APPS_CONFIG = {"alias_drift": {"models": [], "default_connection": "models"}}


@pytest.mark.database_independent
@pytest.mark.asyncio
@pytest.mark.parametrize("composite", [False, True], ids=["scalar", "composite"])
@pytest.mark.parametrize("file_database", [False, True], ids=["memory", "file"])
@pytest.mark.parametrize("aware", [False, True], ids=["naive", "aware"])
@pytest.mark.parametrize("entry", ["state", "alias"])
async def test_drift_uses_the_captured_applications_timezone(tmp_path, composite, file_database, aware, entry):
    default_moment = datetime.datetime(2024, 1, 2, 3, 4, 5, tzinfo=datetime.UTC if aware else None)

    class Entry(Model):
        number = fields.IntField(primary_key=not composite, generated=False)
        region = fields.IntField(default=7)
        if composite:
            pk = fields.CompositePrimaryKey("region", "number")
        moment = fields.DatetimeField(db_default=default_moment)

    app_config = {"library": {"models": [], "default_connection": "default"}}
    url = f"sqlite+aiosqlite://{tmp_path / 'source.sqlite3'}" if file_database else "sqlite+aiosqlite://:memory:"
    async with HareContext() as first:
        await first.init({"connections": {"default": url}, "apps": app_config}, timezone="Europe/Moscow")
        first.register_live_models([Entry], "library")
        await first.generate_schemas()
        client = first.get_connection()
        query = first.apps.get_queryset(Entry)
        await query.create(number=1)
        before = await query.values_list("moment", flat=True)
        state = State.from_models([Entry], app_label="library", environment=MigrationEnvironment.from_apps(first.apps))
        async with HareContext() as second:
            await second.init({"connections": {"default": "sqlite+aiosqlite://:memory:"}, "apps": {}}, timezone="UTC")
            for _ in range(2):
                result = (
                    await detect_drift(client, state, ["library"])
                    if entry == "state"
                    else await detect_drift_for_alias(first.apps, app_config, "default")
                )
                assert not result.has_drift, result
                assert HareContext.require_current() is second
                assert await query.values_list("moment", flat=True) == before


@pytest.mark.sqlite_only
@pytest.mark.asyncio
@pytest.mark.parametrize("composite", [False, True], ids=["scalar", "composite"])
@pytest.mark.parametrize("placement", ["same-alias", "other-alias", "same-table-other-alias"])
async def test_alias_drift_keeps_other_databases_out_of_inventory_and_shadow(composite, placement):
    other_alias = "main" if placement == "same-alias" else "alt"
    same_table = placement == "same-table-other-alias"
    config = {
        "connections": {"main": None, "alt": None},
        "apps": {
            "library": {"models": [], "default_connection": "main"},
            "archive": {"models": [], "default_connection": other_alias},
        },
    }
    async with TemporaryDatabases(config, "sqlite+aiosqlite://:memory:", generate_schemas=False) as context:

        class Book(Model):
            number = fields.IntField(primary_key=not composite, generated=False)
            if composite:
                region = fields.IntField()
                pk = fields.CompositePrimaryKey("region", "number")

            class Meta:
                table = "library_book"

        class ArchivedBook(Model):
            number = fields.IntField(primary_key=not composite, generated=False)
            if composite:
                region = fields.IntField()
                pk = fields.CompositePrimaryKey("region", "number")

            class Meta:
                table = "library_book" if same_table else "archive_book"

        context.register_live_models([Book], "library", "main")
        context.register_live_models([ArchivedBook], "archive", other_alias)
        main = context.get_connection("main")
        other = context.get_connection(other_alias)
        await main.dialect.schema_editor_class(main).create_model(HareContext.require_current().apps.get_binding(Book))
        await other.dialect.schema_editor_class(other).create_model(
            HareContext.require_current().apps.get_binding(ArchivedBook)
        )
        if placement == "other-alias":
            await main.execute_script("CREATE TABLE archive_book (number INTEGER)")
        if same_table:
            await main.execute_script("ALTER TABLE library_book ADD COLUMN rogue INT")
        result = await detect_drift_for_alias(context.apps, config["apps"], "main", app_labels=["library"])
        assert result.operations == []
        assert result.mismatched_objects == []
        assert result.untracked_tables == (["archive_book"] if placement == "other-alias" else [])
        assert result.untracked_columns == ([("library", "Book", "library_book", "rogue")] if same_table else [])
        if same_table:
            other_result = await detect_drift_for_alias(context.apps, config["apps"], "alt", app_labels=["archive"])
            assert not other_result.has_drift
            assert "rogue" not in {row["name"] for row in await other.execute_dicts("PRAGMA table_info(library_book)")}


def alias_drift_context(db_url: str = "sqlite+aiosqlite://:memory:"):
    return hare_test_context(
        modules=["tests.migrations.drift.test_drift_for_alias"],
        db_url=db_url,
        app_label="alias_drift",
        connection_label="models",
    )


async def create_external_table(connection, table: str) -> None:
    await connection.execute_script(
        f"CREATE TABLE IF NOT EXISTS {table} (id INTEGER PRIMARY KEY, title TEXT NOT NULL)"
    )


@pytest.mark.sqlite_only
@pytest.mark.asyncio
async def test_detect_drift_for_alias_reports_no_drift_for_a_managed_false_model():
    async with alias_drift_context() as ctx:
        await create_external_table(ctx.connections.get("models"), '"alias_drift_external"')

        result = await detect_drift_for_alias(ctx.apps, APPS_CONFIG, "models")

        assert isinstance(result, DriftResult)
        assert result.has_drift is False
        assert result.untracked_tables == []


@pytest.mark.sqlite_only
@pytest.mark.asyncio
async def test_detect_drift_for_alias_still_reports_a_real_untracked_table():
    async with alias_drift_context() as ctx:
        connection = ctx.connections.get("models")
        await create_external_table(connection, '"alias_drift_external"')
        await connection.execute_script('CREATE TABLE "alias_drift_stray" (id INTEGER PRIMARY KEY)')

        result = await detect_drift_for_alias(ctx.apps, APPS_CONFIG, "models")

        assert result.untracked_tables == ["alias_drift_stray"]
        assert result.operations == []


@pytest.mark.sqlite_only
@pytest.mark.asyncio
async def test_detect_drift_for_alias_does_not_report_a_live_model_table_as_untracked():
    async with alias_drift_context() as ctx:
        connection = ctx.connections.get("models")
        await create_external_table(connection, '"alias_drift_external"')
        await connection.execute_script('CREATE TABLE "alias_drift_live_rows" (id INTEGER PRIMARY KEY, label TEXT)')

        class AliasDriftLiveRows(Model):
            id = fields.IntField(primary_key=True)
            label = fields.TextField(null=True)

            class Meta:
                table = "alias_drift_live_rows"
                managed = False

        Hare.register_live_models([AliasDriftLiveRows], app_label="alias_drift_live", connection_alias="models")

        result = await detect_drift_for_alias(ctx.apps, APPS_CONFIG, "models")

        assert result.has_drift is False
        assert result.untracked_tables == []


@pytest.mark.sqlite_only
@pytest.mark.asyncio
async def test_detect_drift_for_alias_limits_the_check_to_the_given_app_labels():
    async with alias_drift_context() as ctx:
        connection = ctx.connections.get("models")
        await create_external_table(connection, '"alias_drift_external"')
        await connection.execute_script('CREATE TABLE "alias_drift_live_rows" (id INTEGER PRIMARY KEY, label TEXT)')

        class AliasDriftLiveRowsOnly(Model):
            id = fields.IntField(primary_key=True)
            label = fields.TextField(null=True)

            class Meta:
                table = "alias_drift_live_rows"
                managed = False

        Hare.register_live_models(
            [AliasDriftLiveRowsOnly], app_label="alias_drift_live_only", connection_alias="models"
        )

        result = await detect_drift_for_alias(ctx.apps, APPS_CONFIG, "models", app_labels=["alias_drift"])

        assert result.untracked_tables == ["alias_drift_live_rows"]


@pytest.mark.sqlite_only
@pytest.mark.asyncio
async def test_detect_drift_for_alias_rejects_an_unknown_alias():
    async with alias_drift_context() as ctx:
        with pytest.raises(ConfigurationError, match="Unknown connection 'bogus'"):
            await detect_drift_for_alias(ctx.apps, APPS_CONFIG, "bogus")


@pytest.mark.sqlite_only
@pytest.mark.asyncio
async def test_detect_drift_for_alias_rejects_an_unknown_app_label():
    async with alias_drift_context() as ctx:
        with pytest.raises(ConfigurationError, match="Unknown app label"):
            await detect_drift_for_alias(ctx.apps, APPS_CONFIG, "models", app_labels=["no_such_app"])


@pytest.mark.sqlite_only
@pytest.mark.asyncio
async def test_detect_drift_for_alias_rejects_an_alias_without_apps():
    """A configured connection no app uses has nothing to compare against - every table on it
    would be "untracked", so it's rejected instead."""
    async with alias_drift_context() as ctx:
        apps_config_on_other_alias = {"alias_drift": {"models": [], "default_connection": "other"}}

        with pytest.raises(ConfigurationError, match="No app uses connection 'models'"):
            await detect_drift_for_alias(ctx.apps, apps_config_on_other_alias, "models")


@pytest.mark.asyncio
async def test_detect_drift_for_alias_uses_a_non_default_postgres_schema():
    db_url = DatabaseUnderTest.get_url()
    if not DatabaseUnderTest.get_dialect().features.supports_schemas:
        pytest.skip("A separate database schema is a Postgres-only concept")
    async with alias_drift_context(db_url) as ctx:
        connection = ctx.connections.get("models")
        await connection.execute_script('CREATE SCHEMA IF NOT EXISTS "alias_drift_custom"')
        await create_external_table(connection, '"alias_drift_custom"."alias_drift_external"')
        await connection.execute_script(
            'CREATE TABLE "alias_drift_custom"."alias_drift_stray" (id INTEGER PRIMARY KEY)'
        )
        try:
            result = await detect_drift_for_alias(ctx.apps, APPS_CONFIG, "models", schema="alias_drift_custom")
        finally:
            await connection.execute_script('DROP SCHEMA "alias_drift_custom" CASCADE')

        # Models without Meta.schema are looked up where migrate creates them - the connection's
        # current schema ("public"), so the widget isn't missing; "alias_drift_custom" is only
        # swept for tables, and none of its tables belongs to a model living there.
        assert result.operations == []
        assert result.untracked_tables == ["alias_drift_external", "alias_drift_stray"]


@pytest.mark.asyncio
async def test_detect_drift_for_alias_looks_up_schemaless_models_in_the_search_path_schema():
    db_url = DatabaseUnderTest.get_url()
    if not DatabaseUnderTest.get_dialect().features.supports_schemas:
        pytest.skip("A separate database schema is a Postgres-only concept")
    async with alias_drift_context(db_url) as ctx:
        connection = ctx.connections.get("models")
        await create_external_table(connection, '"alias_drift_external"')
        await connection.execute_script('CREATE SCHEMA IF NOT EXISTS "alias_drift_tenant"')
        try:
            await connection.execute_script('SET search_path TO "alias_drift_tenant"')
            # The table hare creates for the model, in the schema the search path names first.
            editor = connection.dialect.schema_editor_class(connection)
            await connection.execute_script(
                editor.table_creation.get_model_sql_data(
                    HareContext.require_current().apps.get_binding(AliasDriftWidget)
                ).get_table_creation_sql()
            )
            await create_external_table(connection, '"alias_drift_external"')
            result = await detect_drift_for_alias(ctx.apps, APPS_CONFIG, "models")
        finally:
            await connection.execute_script("SET search_path TO public")
            await connection.execute_script('DROP SCHEMA "alias_drift_tenant" CASCADE')

        assert result.operations == []
        assert result.untracked_tables == []


@pytest.mark.sqlite_only
@pytest.mark.asyncio
async def test_detect_drift_for_alias_ignores_schemas_and_extensions_of_other_apps():
    from tests.utils.attached_databases import get_url_with_attached_databases

    db_url = get_url_with_attached_databases("sqlite+aiosqlite://:memory:", ["alias_drift_other_schema"])
    async with alias_drift_context(db_url) as ctx:
        await create_external_table(ctx.connections.get("models"), '"alias_drift_external"')

        class AliasDriftOtherSchemaModel(Model):
            id = fields.IntField(primary_key=True)

            class Meta:
                table = "alias_drift_other_schema_model"
                schema = "alias_drift_other_schema"
                extensions = ["pg_trgm"]

        Hare.register_live_models(
            [AliasDriftOtherSchemaModel], app_label="alias_drift_other", connection_alias="models"
        )

        result = await detect_drift_for_alias(ctx.apps, APPS_CONFIG, "models", app_labels=["alias_drift"])

        assert result.operations == []
