from __future__ import annotations

import os
import sqlite3
import urllib.parse as urlparse
from typing import TYPE_CHECKING, Any

from hare.core.constants import ENV_PYTEST_XDIST_WORKER
from hare.dialects.base.connection_options import ConnectionOptions
from hare.dialects.base.driver import Driver
from hare.dialects.registry import DialectRegistry
from hare.dialects.sqlite.constants import (
    SQLITE_BUSY_RESULT_CODE,
    SQLITE_CONNECTION_OPTIONS,
    SQLITE_DEFAULT_JOURNAL_MODE,
    SQLITE_DEFAULT_JOURNAL_SIZE_LIMIT,
    SQLITE_DIALECT,
    SQLITE_PRIMARY_RESULT_CODE_MASK,
    SQLITE_REGEXP_OPTION,
)
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.sqlite.client.sqlite_client import SqliteClient


class SqliteDriver(Driver):
    """SQLite through the stdlib ``sqlite3`` module."""

    name = "sqlite"
    dialect = SQLITE_DIALECT
    url_schemes = ("sqlite",)
    path_credential = "file_path"
    default_credentials = {
        "journal_mode": SQLITE_DEFAULT_JOURNAL_MODE,
        "journal_size_limit": SQLITE_DEFAULT_JOURNAL_SIZE_LIMIT,
    }
    connection_options = SQLITE_CONNECTION_OPTIONS + ConnectionOptions(SQLITE_REGEXP_OPTION)
    url_has_userinfo = False

    def get_client_class(self, credentials: dict[str, Any]) -> type[SqliteClient]:
        from hare.dialects.sqlite.client.sqlite_client import SqliteClient
        from hare.dialects.sqlite.client.sqlite_client_with_regexp_support import SqliteClientWithRegexpSupport

        install_regexp_functions = SQLITE_REGEXP_OPTION.parse(credentials.get(SQLITE_REGEXP_OPTION.name, False))
        return SqliteClientWithRegexpSupport if install_regexp_functions else SqliteClient

    def get_client_classes(self) -> tuple[type[DatabaseClient], ...]:
        from hare.dialects.sqlite.client.sqlite_client import SqliteClient
        from hare.dialects.sqlite.client.sqlite_client_with_regexp_support import SqliteClientWithRegexpSupport
        from hare.dialects.sqlite.client.sqlite_transaction_client import SqliteTransactionClient

        return SqliteClient, SqliteClientWithRegexpSupport, SqliteTransactionClient

    def is_retryable(self, error: BaseException) -> bool:
        # SQLITE_BUSY and its extended codes (SQLITE_BUSY_SNAPSHOT, SQLITE_BUSY_RECOVERY, ...):
        # another connection holds the lock the statement needs - "database is locked".
        error_code = getattr(error, "sqlite_errorcode", None)
        return (
            isinstance(error, sqlite3.OperationalError)
            and error_code is not None
            and error_code & SQLITE_PRIMARY_RESULT_CODE_MASK == SQLITE_BUSY_RESULT_CODE
        )

    def get_url_path(self, url: urlparse.ParseResult) -> str:
        path = urlparse.unquote(url.netloc + url.path)
        # `sqlite:///C:/dir/db.sqlite` parses to "/C:/dir/db.sqlite", which os.remove() doesn't
        # take as the file SQLite opens.
        if len(path) >= 3 and path[0] == "/" and path[2] == ":" and path[1].isalpha():
            path = path[1:]
        if not path:
            raise ConfigurationError("No path specified for DB_URL")
        return path

    def get_testing_path(self, path: str, reuse_databases: bool) -> tuple[str, dict[str, Any]]:
        # A fixed file is one file for every pytest-xdist worker, and SQLite lets one of them
        # write at a time.
        if path != ":memory:" and "{" not in path:
            xdist_worker_id = os.environ.get(ENV_PYTEST_XDIST_WORKER)
            if xdist_worker_id:
                root, extension = os.path.splitext(path)
                path = f"{root}.{xdist_worker_id}{extension}"
        return super().get_testing_path(path, reuse_databases)


DialectRegistry.register_driver(SqliteDriver())
