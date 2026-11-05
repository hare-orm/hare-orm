from __future__ import annotations

import os
import uuid
from collections.abc import MutableMapping

import pytest

from hare.contrib.test.constants import REUSE_DATABASES_ENVIRONMENT_VARIABLE
from hare.contrib.test.databases.postgresql.constants import POSTGRES_KEPT_TEST_SCHEMA_COMMENT_PREFIX
from hare.contrib.test.databases.resource_cleanup import ResourceCleanup
from hare.core.log import logger
from tests.resources.constants import (
    APPLICATION_NAME_PREFIX,
    TAGGED_DATABASE_PATTERN,
    TEST_DATABASE_ENVIRONMENT_VARIABLE,
    UNTAGGED_URL_ENVIRONMENT_VARIABLE,
)
from tests.resources.messages import TEST_DATABASE_DROP_FAILED, TEST_DATABASE_SWEEP_FAILED
from tests.utils.database_under_test import DatabaseUnderTest
from tests.utils.server_session import ServerSession


class TestRunResources:
    """Resources of one pytest session, including its direct-server advisory lock.

    A random process tag distinguishes concurrent controllers and workers. The server lock
    remains held between test contexts, so another run cannot mistake an idle database for
    an abandoned one. Only tagged databases with an available lock are eligible for sweeping;
    names without that protocol are left alone. Kept schemas use their separate lease lock.
    """

    __test__ = False
    key = pytest.StashKey["TestRunResources"]()

    def __init__(self, environment: MutableMapping[str, str] | None = None) -> None:
        self.environment = os.environ if environment is None else environment
        self.run_tag = uuid.uuid4().hex[:12]
        self.connection: ServerSession | None = None
        self.server_url = ""
        self.previous_environment: dict[str, str | None] = {}
        self.cleanup = ResourceCleanup()

    def configure(self) -> None:
        """Set this process's database template, retaining exact environment values for teardown."""
        for name in (
            TEST_DATABASE_ENVIRONMENT_VARIABLE,
            UNTAGGED_URL_ENVIRONMENT_VARIABLE,
            REUSE_DATABASES_ENVIRONMENT_VARIABLE,
        ):
            self.previous_environment.setdefault(name, self.environment.get(name))
        untagged = self.environment.get(UNTAGGED_URL_ENVIRONMENT_VARIABLE) or self.environment.get(
            TEST_DATABASE_ENVIRONMENT_VARIABLE
        )
        if untagged is not None:
            self.environment[UNTAGGED_URL_ENVIRONMENT_VARIABLE] = untagged
            template = untagged.replace("\\{", "{").replace("\\}", "}")
            if "{}" in template and not DatabaseUnderTest.is_file_database(template):
                self.environment[TEST_DATABASE_ENVIRONMENT_VARIABLE] = template.replace(
                    "{}", f"{self.run_tag}_{{}}", 1
                )
        self.environment.setdefault(REUSE_DATABASES_ENVIRONMENT_VARIABLE, "1")

    def restore_environment(self) -> None:
        """Restore values even when collection failed before asynchronous fixtures started."""
        for name, value in self.previous_environment.items():
            if value is None:
                self.environment.pop(name, None)
            else:
                self.environment[name] = value
        self.previous_environment.clear()

    async def start(self) -> None:
        """Acquire the run lock before any test database can be created.

        Failure to acquire a lock fails setup; continuing would let a concurrent sweep
        delete this run's apparently abandoned databases. Partial startup closes its session.
        """
        self.server_url = self.environment.get(TEST_DATABASE_ENVIRONMENT_VARIABLE, DatabaseUnderTest.DEFAULT_URL)
        if DatabaseUnderTest.get_dialect(self.server_url).name != "postgresql":
            return
        try:
            connection = await self.connect("postgres", f"{APPLICATION_NAME_PREFIX}{self.run_tag}")
            self.cleanup.push(connection.close)
            await connection.execute("SELECT pg_advisory_lock($1)", int(self.run_tag, 16))
            self.connection = connection
            await self.sweep_orphaned_databases()
        except BaseException as error:
            await self.close(error)
            raise

    async def connect(self, database: str, application_name: str | None = None) -> ServerSession:
        """Open a direct server session, bypassing a transaction pooler."""
        return await ServerSession.open(self.server_url, database, application_name)

    async def close(self, error: BaseException | None = None) -> None:
        """Drain releases once, retaining cancellation and both startup and cleanup errors."""
        try:
            await self.cleanup.close()
        finally:
            self.connection = None
        self.cleanup.raise_errors(error)

    async def sweep_orphaned_databases(self) -> None:
        """Best-effort cleanup of tagged databases from completed runs on this server."""
        connection = self.connection
        if connection is None:
            return
        try:
            rows = await connection.fetch(
                "SELECT datname FROM pg_database d WHERE datname LIKE 'test\\_%' "
                "AND NOT EXISTS (SELECT 1 FROM pg_stat_activity a WHERE a.datname = d.datname) "
                "AND coalesce(shobj_description(d.oid, 'pg_database'), '') NOT LIKE $1",
                f"{POSTGRES_KEPT_TEST_SCHEMA_COMMENT_PREFIX}%",
            )
            for row in rows:
                await self.drop_database_if_orphaned(row["datname"])
        except Exception as error:  # noqa: BLE001 - hygiene must not hide the actual test outcome
            logger.warning(TEST_DATABASE_SWEEP_FAILED, error)

    async def drop_database_if_orphaned(self, database_name: str) -> bool:
        """Drop a tagged database only while holding its finished run's advisory lock."""
        connection = self.connection
        match = TAGGED_DATABASE_PATTERN.fullmatch(database_name)
        if connection is None or match is None or match[1] == self.run_tag:
            return False
        lock_key = int(match[1], 16)
        if not await connection.fetchval("SELECT pg_try_advisory_lock($1)", lock_key):
            return False
        try:
            return await self.drop_database(database_name)
        finally:
            await connection.execute("SELECT pg_advisory_unlock($1)", lock_key)

    async def drop_database(self, database_name: str, force: bool = False) -> bool:
        """Roll back prepared transactions and remove a database whose lease is held by the caller."""
        connection = self.connection
        if connection is None:
            return False
        try:
            rows = await connection.fetch("SELECT gid FROM pg_prepared_xacts WHERE database = $1", database_name)
            if rows:
                session = await self.connect(database_name)
                cleanup = ResourceCleanup()
                cleanup.push(session.close)
                try:
                    for row in rows:
                        gid = row["gid"].replace("'", "''")
                        await session.execute(f"ROLLBACK PREPARED '{gid}'")
                except BaseException as error:
                    await cleanup.close()
                    cleanup.raise_errors(error)
                    raise
                else:
                    await cleanup.close()
                    cleanup.raise_errors()
            identifier = database_name.replace('"', '""')
            await connection.execute(f'DROP DATABASE IF EXISTS "{identifier}"{" WITH (FORCE)" if force else ""}')
        except Exception as error:  # noqa: BLE001 - a concurrent connection may prevent DROP
            logger.warning(TEST_DATABASE_DROP_FAILED, database_name, error)
            return False
        return True
