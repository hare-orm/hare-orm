from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.contrib.test.constants import ENV_PYTEST_XDIST_WORKER
from hare.contrib.test.databases.declarations import TestDatabaseIdentity, TestDatabaseLease
from hare.contrib.test.databases.reusable_test_databases import ReusableTestDatabases
from hare.dialects.base.connection.db_url_config_generator import DbUrlConfigGenerator
from hare.dialects.dialect_registry import DialectRegistry
from hare.dialects.postgresql.connection.postgresql_driver import PostgresqlDriver
from hare.dialects.sqlite.connection.sqlite_driver import SqliteDriver

if TYPE_CHECKING:
    from hare.contrib.test.databases.test_database_resource import TestDatabaseResource
    from hare.dialects.base.client.database_client import DatabaseClient


@dataclass(slots=True)
class TestDatabaseConfig:
    """A parsed test configuration and the exact optional lease acquired for it.

    Credentials contain no lease tokens and can be passed to ordinary clients or CLI
    subprocesses. The caller retains this object until cleanup has finished.
    """

    config: dict[str, Any]
    lease: TestDatabaseLease | None = None

    @classmethod
    def from_url(cls, db_url: str, *, reuse: bool = False, keep_schema: bool = False) -> TestDatabaseConfig:
        """Choose a fresh database path, optionally reserving a reusable server slot."""
        config = DbUrlConfigGenerator.expand(db_url)
        driver = DialectRegistry.get_driver(config["engine"])
        credentials = config["credentials"]
        path = credentials[driver.path_credential]
        lease = None
        if path:
            path = path.replace("\\{", "{").replace("\\}", "}")
            if reuse and "{}" in path and isinstance(driver, PostgresqlDriver):
                host = credentials.get("direct_host") or credentials.get("host") or os.environ.get("PGHOST", "")
                port = credentials.get("direct_port") or credentials["port"]
                identity = TestDatabaseIdentity((host, int(port)), path)
                lease = ReusableTestDatabases.acquire(identity, keep_schema=keep_schema)
                path = lease.identity.database
            else:
                if isinstance(driver, SqliteDriver) and path != ":memory:" and "{" not in path:
                    worker = os.environ.get(ENV_PYTEST_XDIST_WORKER)
                    if worker:
                        root, extension = os.path.splitext(path)
                        path = f"{root}.{worker}{extension}"
                path = path.format(uuid.uuid4().hex)
            credentials[driver.path_credential] = path
        return cls(config, lease)

    def bind(self, client: DatabaseClient) -> TestDatabaseResource:
        """Bind lifecycle operations to the acquired client, outside production drivers."""
        from hare.contrib.test.databases.test_database_resource import TestDatabaseResource

        if self.lease is not None:
            from hare.contrib.test.databases.postgresql.test_database import PostgresqlTestDatabase

            return PostgresqlTestDatabase(client, self.lease)  # type: ignore[arg-type]
        return TestDatabaseResource(client)
