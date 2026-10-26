from __future__ import annotations

import hashlib
import itertools
import os
import uuid
from typing import Any

from hare.contrib.test import hare_test_context
from hare.contrib.test.constants import REUSABLE_DATABASE_SLOT_NAMESPACE
from hare.core.hare_context import HareContext
from hare.dialects.postgresql.client.reusable_test_schemas import ReusableTestSchemas
from hare.exceptions import ConfigurationError


class KeptSharedTestDatabase:
    """The PostgreSQL database a test process shares between its test modules, kept from one test run
    to the next: one name per pytest-xdist worker, taken under an advisory lock - a concurrent run
    gets a name of its own - and a later run takes the database when it holds the schema kept in its
    comment, unchanged since. Creating the database and its schema of the ~135 test models cost every
    worker of every run seconds before its first test."""

    @staticmethod
    def get_modules() -> list[str]:
        """The models of the shared database - on PostgreSQL its own models too, those of the fields
        only it has (a DecimalField, which SQLite has none of)."""
        # Local import: the database under test reads the configured URL.
        from tests.utils.database_under_test import DatabaseUnderTest

        modules = ["tests.shared_models.testmodels"]
        if DatabaseUnderTest.get_dialect().name == "postgresql":
            modules.append("tests.shared_models.testmodels_postgres")
        return modules

    @staticmethod
    async def acquire_url(ownership_connection: Any, database_url_template: str) -> str:
        """The URL of this worker's kept database, its name taken under an advisory lock held by the
        ownership connection for the whole session.

        Args:
            ownership_connection: The asyncpg connection holding the session's locks.
            database_url_template: The database URL with one "{}" placeholder.

        Returns:
            The URL.
        """
        worker_id = os.environ.get("PYTEST_XDIST_WORKER", "")
        database_id = uuid.uuid5(REUSABLE_DATABASE_SLOT_NAMESPACE, f"shared:{worker_id}").hex
        for number in itertools.count():
            tag = hashlib.sha256(f"shared:{worker_id}:{number}".encode()).hexdigest()[:12]
            if await ownership_connection.fetchval("SELECT pg_try_advisory_lock($1)", int(tag, 16)):
                return database_url_template.replace("{}", f"{tag}_{database_id}", 1)
        raise AssertionError("unreachable")

    @classmethod
    async def take(cls, database_url: str) -> bool:
        """Takes the database at the URL as it is, its rows cleared, when it holds the schema kept in
        its comment.

        Args:
            database_url: The database's URL.

        Returns:
            True when the database was taken; False when it is missing or has to be created again.
        """
        try:
            async with hare_test_context(
                modules=cls.get_modules(),
                db_url=database_url,
                app_label="models",
                connection_label="models",
                _create_db=False,
                _generate_schemas=False,
                _drop_db_on_exit=False,
            ) as context:
                client = context.get_connection()
                kept_schema = await ReusableTestSchemas.read_kept_schema(client)
                if kept_schema is None or kept_schema.schema_key != cls.get_schema_key(context):
                    return False
                cleared_schema = await ReusableTestSchemas.clear_rows(client, kept_schema)
                if cleared_schema is None:
                    return False
                await ReusableTestSchemas.write_kept_schema(client, cleared_schema)
                return True
        except ConfigurationError:
            # The database does not exist.
            return False

    @classmethod
    async def keep(cls, context: HareContext) -> None:
        """Keeps the schema the context just created in its database's comment.

        Args:
            context: The context of the database.
        """
        client = context.get_connection()
        kept_schema = await ReusableTestSchemas.get_kept_schema(client, cls.get_schema_key(context))
        await ReusableTestSchemas.write_kept_schema(client, kept_schema)

    @staticmethod
    def get_schema_key(context: HareContext) -> str:
        """The digest of the DDL creating the context's schema.

        Args:
            context: The context.

        Returns:
            The digest.
        """
        return ReusableTestSchemas.get_schema_key(context.get_connection().get_schema_sql(safe=False))
