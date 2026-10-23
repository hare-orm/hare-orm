"""The database the test suite runs on - HARE_TEST_DB - described by its driver and dialect rather
than by the text of its URL."""

import os

import pytest

from hare.dialects.base.connection.db_url_config_generator import DbUrlConfigGenerator
from hare.dialects.base.connection.driver import Driver
from hare.dialects.base.dialect import Dialect
from hare.dialects.dialect_registry import DialectRegistry
from hare.dialects.sqlite.sqlite_dialect import SqliteDialect


class DatabaseUnderTest:
    """The database HARE_TEST_DB points at."""

    DEFAULT_URL = "sqlite+aiosqlite://:memory:"

    @classmethod
    def get_url(cls) -> str:
        """The configured DB_URL, SQLite in memory when none is."""
        return os.getenv("HARE_TEST_DB", cls.DEFAULT_URL)

    @classmethod
    def get_driver(cls, url: str | None = None) -> Driver:
        """The driver a DB_URL's scheme picks - HARE_TEST_DB's by default."""
        return DialectRegistry.get_driver_for_url_scheme((url or cls.get_url()).split("://", 1)[0])

    @classmethod
    def get_dialect(cls, url: str | None = None) -> Dialect:
        """The dialect of a DB_URL - HARE_TEST_DB's by default."""
        return cls.get_driver(url).dialect

    @classmethod
    def get_public_schema(cls) -> str | None:
        """PostgreSQL's ``public`` schema; None - the connection's default database - elsewhere."""
        return "public" if cls.get_dialect().name == "postgresql" else None

    @classmethod
    def is_file_database(cls, url: str | None = None) -> bool:
        """Whether the database is a file (or ``:memory:``) the test opens itself, rather than a
        database server that creates and drops databases."""
        return cls.get_driver(url).path_credential == "file_path"

    @classmethod
    def pools_server_sessions(cls, url: str | None = None) -> bool:
        """Whether the connection goes through a transaction pooler (``transaction_pooling``) - its
        server sessions are lent out a transaction at a time, so what a test pins on one session
        (a backend's pid, a database setting new sessions pick up) doesn't hold."""
        credentials = DbUrlConfigGenerator.expand(url or cls.get_url(), testing=False)["credentials"]
        return str(credentials.get("transaction_pooling", "")).lower() in ("true", "1")

    @staticmethod
    def get_direct_credentials(credentials: dict[str, object]) -> dict[str, object]:
        """Connection credentials of the server itself, past a transaction pooler - the same ones
        for a connection going straight to it."""
        direct_host = credentials.get("direct_host")
        if direct_host is None:
            return credentials
        direct_credentials = {
            key: value
            for key, value in credentials.items()
            if key not in ("transaction_pooling", "direct_host", "direct_port")
        }
        direct_credentials["host"] = direct_host
        direct_credentials["port"] = credentials.get("direct_port") or credentials.get("port")
        return direct_credentials

    @classmethod
    async def get_postgresql_server_version(cls) -> tuple[int]:
        """The major version of the PostgreSQL server HARE_TEST_DB points at, asked of the server
        itself - before a context opens, which an older server may refuse."""
        import asyncpg

        credentials = cls.get_direct_credentials(
            DbUrlConfigGenerator.expand(cls.get_url(), testing=False)["credentials"]
        )
        connection = await asyncpg.connect(
            user=credentials["user"],
            password=credentials["password"],
            host=credentials["host"],
            port=credentials["port"],
            database="postgres",
        )
        try:
            version_number = int(await connection.fetchval("SHOW server_version_num"))
        finally:
            await connection.close()
        return (version_number // 10000,)

    @classmethod
    async def skip_if_the_pooler_loses_the_search_path(cls) -> None:
        """Skips a test setting a search path (the schema option, a schema per tenant) behind a
        transaction pooler on PostgreSQL before 18, which doesn't tell the pooler its search path -
        hare refuses such a connection."""
        if cls.pools_server_sessions() and await cls.get_postgresql_server_version() < (18,):
            pytest.skip("PostgreSQL before 18 doesn't tell a transaction pooler its search path")

    @staticmethod
    def get_engine_name(dialect: Dialect) -> str:
        """The database engine a dialect runs on - ``sqlite`` for SQLite and the dialects built on
        its engine, else the dialect's name. A test reading the engine's own catalog (``PRAGMA``,
        ``pg_indexes``) branches on it."""
        return "sqlite" if isinstance(dialect, SqliteDialect) else dialect.name
