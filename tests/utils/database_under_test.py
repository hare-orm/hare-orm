"""The database the test suite runs on - HARE_TEST_DB - described by its driver and dialect rather
than by the text of its URL."""

from __future__ import annotations

import os
import uuid
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING

import pytest

from hare.dialects.base.connection.db_url_config_generator import DbUrlConfigGenerator
from hare.dialects.base.connection.driver import Driver
from hare.dialects.base.dialect import Dialect
from hare.dialects.dialect_registry import DialectRegistry
from hare.dialects.sqlite.sqlite_dialect import SqliteDialect
from hare.fields.data.numeric.decimal_field import DecimalField
from tests.utils.constants import CLICKHOUSE_PROFILE_SKIP_REASON

if TYPE_CHECKING:
    from pathlib import Path

    from hare.core.hare_context import HareContext


class DatabaseUnderTest:
    """The database HARE_TEST_DB points at."""

    DEFAULT_URL = "sqlite+aiosqlite://:memory:"

    @classmethod
    def get_url(cls) -> str:
        """The configured DB_URL, SQLite in memory when none is."""
        return os.getenv("HARE_TEST_DB", cls.DEFAULT_URL)

    @classmethod
    def open_model_context(
        cls, modules: list[str], *, db_url: str | None = None, generate_schemas: bool = True
    ) -> AbstractAsyncContextManager[HareContext]:
        """Open an isolated context whose models use the test database's ``models`` alias."""
        from hare.contrib.test import hare_test_context

        return hare_test_context(
            modules,
            db_url=cls.get_url() if db_url is None else db_url,
            connection_label="models",
            _generate_schemas=generate_schemas,
        )

    @staticmethod
    def get_sqlite_file_url(path: Path, *, scheme: str = "sqlite+aiosqlite") -> str:
        """The URL of a test SQLite file with synchronous writes disabled."""
        return f"{scheme}:///{path}?synchronous=OFF"

    @classmethod
    def open_file_model_context(
        cls, path: Path, *, scheme: str = "sqlite+aiosqlite"
    ) -> AbstractAsyncContextManager[HareContext]:
        """Open the shared test models on an explicit SQLite file."""
        return cls.open_model_context(
            ["tests.shared_models.testmodels"], db_url=cls.get_sqlite_file_url(path, scheme=scheme)
        )

    @classmethod
    def open_persistent_model_context(cls, path: Path) -> AbstractAsyncContextManager[HareContext]:
        """Open the shared test models on the test server or on a temporary SQLite file."""
        url = cls.get_url()
        if not url or cls.is_file_database(url):
            scheme = (url or cls.DEFAULT_URL).split("://", 1)[0]
            return cls.open_file_model_context(path, scheme=scheme)
        return cls.open_model_context(["tests.shared_models.testmodels"], db_url=url)

    @staticmethod
    def normalize_url_template(url: str) -> str:
        """Read the Makefile's escaped placeholders as ordinary braces."""
        return url.replace("\\{", "{").replace("\\}", "}")

    @classmethod
    def get_postgresql_url_template(cls) -> str | None:
        """The configured test URL with unescaped placeholders, when it uses PostgreSQL."""
        url = cls.get_url()
        if cls.get_dialect(url).name != "postgresql":
            return None
        return cls.normalize_url_template(url)

    @classmethod
    def get_fresh_url(cls, url: str | None = None) -> str:
        """Expand an optional database-name placeholder for an isolated test context.

        Use the supplied URL or read HARE_TEST_DB, accepting the Makefile's escaped braces. URLs
        without a placeholder keep their value; a template receives a fresh UUID.
        """
        raw_db_url = cls.normalize_url_template(cls.get_url() if url is None else url)
        return raw_db_url.format(uuid.uuid4().hex) if "{}" in raw_db_url else raw_db_url

    @classmethod
    def get_url_with_options(cls, options: str, url: str | None = None) -> str:
        """A DB_URL - HARE_TEST_DB's by default - with more options of its query, after those it has."""
        base_url = url or cls.get_url()
        return f"{base_url}{'&' if '?' in base_url else '?'}{options}"

    @classmethod
    def get_driver(cls, url: str | None = None) -> Driver:
        """The driver a DB_URL's scheme picks - HARE_TEST_DB's by default."""
        return DialectRegistry.get_driver_for_url_scheme((url or cls.get_url()).split("://", 1)[0])

    @classmethod
    def get_dialect(cls, url: str | None = None) -> Dialect:
        """The dialect of a DB_URL - HARE_TEST_DB's by default."""
        return cls.get_driver(url).dialect

    @classmethod
    def holds_decimal_fields(cls) -> bool:
        """Whether the database has a DecimalField - SQLite stores no exact decimal. A test model declares
        its DecimalField under this condition, and a test of the field is marked decimal_fields."""
        return DecimalField(max_digits=1, decimal_places=0).exists_on(cls.get_dialect())

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
        credentials = DbUrlConfigGenerator.expand(url or cls.get_url())["credentials"]
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
        # Local import: the session module reads this one's direct credentials.
        from tests.utils.server_session import ServerSession

        session = await ServerSession.open(cls.get_url(), "postgres")
        try:
            version_number = int(await session.fetchval("SHOW server_version_num"))
        finally:
            await session.close()
        return (version_number // 10000,)

    @classmethod
    async def skip_if_the_pooler_loses_the_search_path(cls) -> None:
        """Skips a test setting a search path (the schema option, a schema per tenant) behind a
        transaction pooler on PostgreSQL before 18, which doesn't tell the pooler its search path -
        hare refuses such a connection."""
        if cls.pools_server_sessions() and await cls.get_postgresql_server_version() < (18,):
            pytest.skip("PostgreSQL before 18 doesn't tell a transaction pooler its search path")

    @staticmethod
    def skip_unless_clickhouse(
        database_url: str | None = None, *, reason: str = CLICKHOUSE_PROFILE_SKIP_REASON
    ) -> None:
        """Skip outside a ClickHouse profile, preserving a supplied URL snapshot and diagnostic."""
        if database_url is None:
            database_url = os.getenv("HARE_TEST_DB", "")
        if not database_url.startswith("clickhouse"):
            pytest.skip(reason)

    @staticmethod
    def get_engine_name(dialect: Dialect) -> str:
        """The database engine a dialect runs on - ``sqlite`` for SQLite and the dialects built on
        its engine, else the dialect's name. A test reading the engine's own catalog (``PRAGMA``,
        ``pg_indexes``) branches on it."""
        return "sqlite" if isinstance(dialect, SqliteDialect) else dialect.name
