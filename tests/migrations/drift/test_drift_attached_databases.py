"""Drift of a model whose ``Meta.schema`` names an attached database on SQLite."""

from __future__ import annotations

import pytest

from hare import fields
from hare.contrib.test.isolated_contexts import hare_test_context
from hare.migrations.drift import detect_drift_for_alias
from hare.migrations.operations import AddField
from hare.models import Model
from tests.utils.attached_databases import get_url_with_attached_databases

ARCHIVE_DATABASE = "attached_drift_archive"


class AttachedDriftNote(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=50)
    body = fields.TextField()

    class Meta:
        app = "attached_drift"
        table = "attached_drift_note"
        schema = ARCHIVE_DATABASE


APPS_CONFIG = {"attached_drift": {"models": [], "default_connection": "models"}}


def attached_drift_context():
    return hare_test_context(
        modules=["tests.migrations.drift.test_drift_attached_databases"],
        db_url=get_url_with_attached_databases("sqlite+aiosqlite://:memory:", [ARCHIVE_DATABASE]),
        app_label="attached_drift",
        connection_label="models",
    )


@pytest.mark.sqlite_only
@pytest.mark.asyncio
async def test_a_model_of_an_attached_database_matches_its_table_there():
    async with attached_drift_context() as ctx:
        result = await detect_drift_for_alias(ctx.apps, APPS_CONFIG, "models")

        assert not result.has_drift


@pytest.mark.sqlite_only
@pytest.mark.asyncio
async def test_a_column_missing_from_a_table_of_an_attached_database_is_drift():
    async with attached_drift_context() as ctx:
        connection = ctx.connections.get("models")
        await connection.execute_script(f'ALTER TABLE "{ARCHIVE_DATABASE}"."attached_drift_note" DROP COLUMN "body"')

        result = await detect_drift_for_alias(ctx.apps, APPS_CONFIG, "models")

        assert [(type(operation), operation.name) for operation in result.operations] == [(AddField, "body")]


@pytest.mark.sqlite_only
@pytest.mark.asyncio
async def test_a_table_of_the_same_name_in_the_main_database_is_not_the_models():
    async with attached_drift_context() as ctx:
        connection = ctx.connections.get("models")
        await connection.execute_script('CREATE TABLE "attached_drift_note" ("id" INTEGER PRIMARY KEY)')

        result = await detect_drift_for_alias(ctx.apps, APPS_CONFIG, "models")

        assert result.operations == []
        assert result.untracked_tables == ["attached_drift_note"]


@pytest.mark.sqlite_only
@pytest.mark.asyncio
async def test_an_untracked_table_of_an_attached_database_is_found_by_its_schema():
    async with attached_drift_context() as ctx:
        connection = ctx.connections.get("models")
        await connection.execute_script(f'CREATE TABLE "{ARCHIVE_DATABASE}"."attached_drift_stray" ("id" INTEGER)')

        result = await detect_drift_for_alias(ctx.apps, APPS_CONFIG, "models", schema=ARCHIVE_DATABASE)

        assert result.untracked_tables == ["attached_drift_stray"]
