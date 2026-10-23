"""drift of a table whose UNIQUE was declared without a name - the database named its index itself
(PostgreSQL ``<table>_<columns>_key``, SQLite ``sqlite_autoindex_<table>_<n>``), and the model ``inspectdb``
writes for it declares ``unique=True``: it is the index hare names otherwise, no drift."""

import os

import pytest

from hare import fields
from hare.contrib.test.isolated_contexts import hare_test_context
from hare.core.connections.connections import Connections
from hare.migrations.autodetection.migration_autodetector import MigrationAutodetector
from hare.migrations.drift.detect_drift import detect_drift
from hare.models import Model
from tests.utils.database_under_test import DatabaseUnderTest

APP_LABEL = "drift_database_named_indexes"


class DriftInlineUnique(Model):
    id = fields.IntField(primary_key=True, generated=False)
    name = fields.TextField(unique=True)
    count = fields.IntField(default=0)

    class Meta:
        table = "drift_inline_unique"
        app = APP_LABEL


@pytest.mark.asyncio
async def test_a_unique_the_database_named_is_the_fields_own():
    """The database's index was removed and the field's added - SQLite's also read without its key order."""
    if not DatabaseUnderTest.get_dialect().features.supports_unique_constraints:
        pytest.skip("the database keeps nothing unique")
    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    async with hare_test_context(
        modules=[__name__], db_url=db_url, app_label=APP_LABEL, connection_label="models", _generate_schemas=False
    ) as ctx:
        connection = Connections.get("models")
        # INTEGER is a column SQLite reads as the INT an IntField declares - the same column.
        await connection.execute_script(
            "CREATE TABLE drift_inline_unique (id int PRIMARY KEY, name text NOT NULL UNIQUE, count INTEGER NOT NULL)"
        )
        apps_config = {APP_LABEL: {"models": [], "default_connection": "models"}}
        new_state = MigrationAutodetector(ctx.apps, apps_config).current_state()

        result = await detect_drift(connection, new_state, [APP_LABEL], schema=DatabaseUnderTest.get_public_schema())

        assert [operation.describe() for operation in result.operations] == []
