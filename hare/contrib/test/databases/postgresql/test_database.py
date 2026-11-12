from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from hare.contrib.test.databases.declarations import TestDatabaseLease
from hare.contrib.test.databases.kept_test_schema import KeptTestSchema
from hare.contrib.test.databases.postgresql.constants import (
    POSTGRES_CREATE_LEGACY_PUBLIC_SCHEMA_STATEMENTS,
    POSTGRES_CREATE_PUBLIC_SCHEMA_STATEMENTS,
    POSTGRES_DATABASE_EXISTS_SQL,
    POSTGRES_DATABASE_OWNER_PUBLIC_SCHEMA_VERSION_NUMBER,
    POSTGRES_DATABASE_PREPARED_TRANSACTIONS_SQL,
    POSTGRES_DATABASE_ROLE_SETTINGS_SQL,
    POSTGRES_DROP_SCHEMA_CASCADE_SQL,
    POSTGRES_RESET_DATABASE_ROLE_SETTINGS_SQL,
    POSTGRES_RESET_DATABASE_SETTINGS_SQL,
    POSTGRES_ROLLBACK_PREPARED_SQL,
    POSTGRES_TERMINATE_OTHER_DATABASE_SESSIONS_SQL,
    POSTGRES_USER_SCHEMAS_SQL,
)
from hare.contrib.test.databases.postgresql.reusable_test_schemas import ReusableTestSchemas
from hare.contrib.test.databases.resource_cleanup import ResourceCleanup
from hare.contrib.test.databases.reusable_test_databases import ReusableTestDatabases
from hare.contrib.test.databases.test_database_resource import TestDatabaseResource
from hare.core.resource_completion import ResourceCompletion
from hare.dialects.base.client.schema_cache_invalidation import SchemaCacheInvalidation
from hare.dialects.postgresql.client.postgresql_client import PostgresqlClient
from hare.dialects.postgresql.client.transaction_pooler import TransactionPooler
from hare.dialects.postgresql.constants import POSTGRES_SERVER_VERSION_NUMBER_SQL
from hare.exceptions import ConfigurationError


class PostgresqlTestDatabase(TestDatabaseResource):
    """Reuse a leased PostgreSQL database; production clients contain no test state."""

    client: PostgresqlClient

    def __init__(self, client: PostgresqlClient, lease: TestDatabaseLease) -> None:
        super().__init__(client)
        self.lease = lease
        self.kept_test_schema: KeptTestSchema | None = None
        self.leased_test_schema: KeptTestSchema | None = None

    async def create(self) -> None:
        """Makes a leased test database exist and be empty: reuses it as is when this process
        already reset it, takes the schema it kept for a context creating its models' schema
        (``generate_schema()`` decides on it), resets it when it exists from before, creates it
        otherwise."""
        assert self.client.database is not None and self.lease is not None  # nosec B101
        self.leased_test_schema = None
        is_clean, self.kept_test_schema = ReusableTestDatabases.claim(self.lease)
        if self.kept_test_schema is None and not is_clean:
            if await self._database_exists():
                await self._reset_database()
            else:
                await self.client._create_database()

    async def release(self) -> None:
        """Returns a leased test database to the pool: keeps the schema its context created, the rows
        cleared, when the schema is unchanged, and resets the database otherwise - also when that
        fails, leaving it marked as needing a reset before its next use."""
        assert self.client.database is not None and self.lease is not None  # nosec B101
        if not ReusableTestDatabases.is_leased(self.lease):
            return  # already released - the slot may belong to another context by now
        leased_test_schema, self.leased_test_schema = self.leased_test_schema, None
        self.kept_test_schema = None
        is_clean = False
        kept_schema = None
        try:
            if leased_test_schema is not None:
                kept_schema = await self._clear_rows(leased_test_schema)
            if kept_schema is None:
                is_clean = await self._reset_database()
        finally:
            ReusableTestDatabases.release(self.lease, is_clean=is_clean, kept_schema=kept_schema)

    async def generate_schema(self, safe: bool = False) -> None:
        """Creates every model of this connection in the database - on a leased test database holding
        the same schema from its previous lease, nothing: the schema stands, its rows cleared.

        Args:
            safe: Whether each object is created only when it doesn't exist yet.
        """
        # Connected first, so the DDL is written for the features of the server version.
        async with self.client.acquire_connection():
            pass
        schema = self.client.get_schema_sql(safe)
        schema_key = ReusableTestSchemas.get_schema_key(schema)
        kept_test_schema, self.kept_test_schema = self.kept_test_schema, None
        if kept_test_schema is not None:
            if kept_test_schema.schema_key == schema_key:
                self.leased_test_schema = kept_test_schema
                return
            await self._reset_database()
        self.client.log.debug("Creating schema: %s", schema)
        if schema:
            async with SchemaCacheInvalidation.run(self.client):
                await self.client.execute_built_script(schema)
        # Only the schema a context creates first thing is kept - a later call may follow DDL of the test.
        if self.leased_test_schema is None and self.lease.keep_schema:
            self.leased_test_schema = await ReusableTestSchemas.get_kept_schema(self.client, schema_key)

    async def _database_exists(self) -> bool:
        """Whether the configured database exists on the server.

        Returns:
            True if pg_database has it.
        """
        async with self._server_session() as client:
            await client._reconnect(with_db=False)
            rows = await client.execute_dicts(POSTGRES_DATABASE_EXISTS_SQL, [client.database])
        return bool(rows)

    @asynccontextmanager
    async def _server_session(self) -> AsyncGenerator[PostgresqlClient]:
        """Own an independent reset client using the configured direct server endpoint.

        Retire the application's pool before resetting its database. The reset client uses the
        common pooler settings and closes even if setup or cleanup fails; the application's host,
        port and connection options are never temporarily changed.
        """
        settings = TransactionPooler.get_direct_settings(self.client)
        await ResourceCompletion.wait(self.client.close())
        client: PostgresqlClient = self.client.create_independent_client({**settings, "min_size": 0})  # type: ignore[assignment]
        cleanup = ResourceCleanup()
        cleanup.push(client.close)
        original: BaseException | None = None
        try:
            yield client
        except BaseException as error:
            original = error
            raise
        finally:
            await cleanup.close()
            cleanup.raise_errors(original)

    @staticmethod
    async def _end_other_sessions(client: PostgresqlClient) -> None:
        """Ends the database's other sessions and rolls back its prepared transactions."""
        await client.execute(POSTGRES_TERMINATE_OTHER_DATABASE_SESSIONS_SQL)
        for prepared_transaction_row in await client.execute_dicts(POSTGRES_DATABASE_PREPARED_TRANSACTIONS_SQL):
            gid_literal = client.dialect.literals.get_string_literal_sql(prepared_transaction_row["gid"])
            await client.execute_script(POSTGRES_ROLLBACK_PREPARED_SQL.format(gid_literal=gid_literal))

    @staticmethod
    async def _get_settings_reset_statements(client: PostgresqlClient) -> list[str]:
        """The statements clearing the database's settings and its roles' settings in it.

        Returns:
            The statements.
        """
        role_setting_rows = await client.execute_dicts(POSTGRES_DATABASE_ROLE_SETTINGS_SQL)
        quoted_database = client.dialect.literals.quote_identifier(client.database or "")
        statements = [POSTGRES_RESET_DATABASE_SETTINGS_SQL.format(database=quoted_database)]
        statements.extend(
            POSTGRES_RESET_DATABASE_ROLE_SETTINGS_SQL.format(
                role=role_setting_row["role_name"], database=quoted_database
            )
            for role_setting_row in role_setting_rows
        )
        return statements

    async def _clear_rows(self, leased_test_schema: KeptTestSchema) -> KeptTestSchema | None:
        """Empties a leased test database whose schema is unchanged, keeping the schema - on the
        independent reset session.

        Args:
            leased_test_schema: The schema the lease's context created.

        Returns:
            The schema kept, or None when the database has to be reset instead.
        """
        async with self._server_session() as client:
            try:
                await client._reconnect(with_db=True)
            except ConfigurationError:
                return None
            await self._end_other_sessions(client)
            kept_schema = await ReusableTestSchemas.clear_rows(client, leased_test_schema)
            if kept_schema is not None:
                await client.execute_script(";\n".join(await self._get_settings_reset_statements(client)))
            return kept_schema

    async def _reset_database(self) -> bool:
        """Reset the database through an independent direct server session.

        End other sessions and prepared transactions, drop non-system schemas, recreate the
        public schema and clear database-level and role settings without dropping the database.

        Returns:
            False when the database does not exist, True once it was reset.
        """
        async with self._server_session() as client:
            try:
                await client._reconnect(with_db=True)
            except ConfigurationError:
                if not await self._database_exists():
                    return False
                raise
            await self._end_other_sessions(client)
            schema_rows = await client.execute_dicts(POSTGRES_USER_SCHEMAS_SQL)
            version_rows = await client.execute_dicts(POSTGRES_SERVER_VERSION_NUMBER_SQL)
            statements = [
                POSTGRES_DROP_SCHEMA_CASCADE_SQL.format(
                    schema=client.dialect.literals.quote_identifier(schema_row["nspname"])
                )
                for schema_row in schema_rows
            ]
            if version_rows[0]["server_version_number"] >= POSTGRES_DATABASE_OWNER_PUBLIC_SCHEMA_VERSION_NUMBER:
                statements.extend(POSTGRES_CREATE_PUBLIC_SCHEMA_STATEMENTS)
            else:
                statements.extend(POSTGRES_CREATE_LEGACY_PUBLIC_SCHEMA_STATEMENTS)
            statements.extend(await self._get_settings_reset_statements(client))
            await client.execute_script(";\n".join(statements))
        return True
