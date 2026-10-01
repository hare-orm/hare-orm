"""Tests for detect_drift_for_alias(), the one-call drift check for a single connection alias."""

from __future__ import annotations

import os

import pytest

from hare import Hare, fields
from hare.contrib.test.helpers import hare_test_context
from hare.exceptions import ConfigurationError
from hare.migrations.drift import DriftResult, detect_drift_for_alias
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


def alias_drift_context(db_url: str = "sqlite://:memory:"):
    return hare_test_context(
        modules=["tests.migrations.test_drift_for_alias"],
        db_url=db_url,
        app_label="alias_drift",
        connection_label="models",
    )


async def create_external_table(connection, table: str) -> None:
    await connection.execute_script(
        f"CREATE TABLE IF NOT EXISTS {table} (id INTEGER PRIMARY KEY, title TEXT NOT NULL)"
    )


@pytest.mark.asyncio
async def test_detect_drift_for_alias_reports_no_drift_for_a_managed_false_model():
    async with alias_drift_context() as ctx:
        await create_external_table(ctx.connections.get("models"), '"alias_drift_external"')

        result = await detect_drift_for_alias(ctx.apps, APPS_CONFIG, "models")

        assert isinstance(result, DriftResult)
        assert result.has_drift is False
        assert result.untracked_tables == []


@pytest.mark.asyncio
async def test_detect_drift_for_alias_still_reports_a_real_untracked_table():
    async with alias_drift_context() as ctx:
        connection = ctx.connections.get("models")
        await create_external_table(connection, '"alias_drift_external"')
        await connection.execute_script('CREATE TABLE "alias_drift_stray" (id INTEGER PRIMARY KEY)')

        result = await detect_drift_for_alias(ctx.apps, APPS_CONFIG, "models")

        assert result.untracked_tables == ["alias_drift_stray"]
        assert result.operations == []


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


@pytest.mark.asyncio
async def test_detect_drift_for_alias_rejects_an_unknown_alias():
    async with alias_drift_context() as ctx:
        with pytest.raises(ConfigurationError, match='Unknown connection "bogus"'):
            await detect_drift_for_alias(ctx.apps, APPS_CONFIG, "bogus")


@pytest.mark.asyncio
async def test_detect_drift_for_alias_rejects_an_unknown_app_label():
    async with alias_drift_context() as ctx:
        with pytest.raises(ConfigurationError, match="Unknown app label"):
            await detect_drift_for_alias(ctx.apps, APPS_CONFIG, "models", app_labels=["no_such_app"])


@pytest.mark.asyncio
async def test_detect_drift_for_alias_rejects_an_alias_without_apps():
    """A configured connection no app uses has nothing to compare against - every table on it
    would be "untracked", so it's rejected instead."""
    async with alias_drift_context() as ctx:
        apps_config_on_other_alias = {"alias_drift": {"models": [], "default_connection": "other"}}

        with pytest.raises(ConfigurationError, match='No app uses connection "models"'):
            await detect_drift_for_alias(ctx.apps, apps_config_on_other_alias, "models")


@pytest.mark.asyncio
async def test_detect_drift_for_alias_uses_a_non_default_postgres_schema():
    db_url = os.getenv("HARE_TEST_DB", "sqlite://:memory:")
    if not DatabaseUnderTest.get_dialect().supports_schemas:
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
    db_url = os.getenv("HARE_TEST_DB", "sqlite://:memory:")
    if not DatabaseUnderTest.get_dialect().supports_schemas:
        pytest.skip("A separate database schema is a Postgres-only concept")
    async with alias_drift_context(db_url) as ctx:
        connection = ctx.connections.get("models")
        await create_external_table(connection, '"alias_drift_external"')
        await connection.execute_script('CREATE SCHEMA IF NOT EXISTS "alias_drift_tenant"')
        try:
            await connection.execute_script('SET search_path TO "alias_drift_tenant"')
            await connection.execute_script(
                'CREATE TABLE "alias_drift_widget" (id INTEGER PRIMARY KEY, name VARCHAR(50) NOT NULL, '
                "code VARCHAR(10) UNIQUE, slug VARCHAR(10) UNIQUE)"
            )
            await connection.execute_script('CREATE INDEX "alias_drift_tenant_slug" ON "alias_drift_widget" (slug)')
            await create_external_table(connection, '"alias_drift_external"')
            result = await detect_drift_for_alias(ctx.apps, APPS_CONFIG, "models")
        finally:
            await connection.execute_script("SET search_path TO public")
            await connection.execute_script('DROP SCHEMA "alias_drift_tenant" CASCADE')

        assert result.operations == []
        assert result.untracked_tables == []


@pytest.mark.asyncio
async def test_detect_drift_for_alias_ignores_schemas_and_extensions_of_other_apps():
    async with alias_drift_context() as ctx:
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
