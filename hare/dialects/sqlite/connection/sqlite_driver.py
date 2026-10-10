from __future__ import annotations

import os
import urllib.parse as urlparse
from typing import Any

from hare.core.constants import ENV_PYTEST_XDIST_WORKER
from hare.dialects.base.connection.connection_options import ConnectionOptions
from hare.dialects.base.connection.driver import Driver
from hare.dialects.sqlite.connection.constants import SQLITE_BUSY_RESULT_CODE, SQLITE_PRIMARY_RESULT_CODE_MASK
from hare.dialects.sqlite.constants import (
    SQLITE_CONNECTION_OPTIONS,
    SQLITE_DEFAULT_JOURNAL_MODE,
    SQLITE_DEFAULT_JOURNAL_SIZE_LIMIT,
    SQLITE_DIALECT,
    SQLITE_REGEXP_OPTION,
)
from hare.exceptions import ConfigurationError


class SqliteDriver(Driver):
    """What every SQLite driver shares: the database file of the DB_URL, its PRAGMA settings and a
    busy database to retry on. A driver of ``hare.dialects.sqlite.drivers`` names its client."""

    dialect = SQLITE_DIALECT
    path_credential = "file_path"
    default_credentials = {
        "journal_mode": SQLITE_DEFAULT_JOURNAL_MODE,
        "journal_size_limit": SQLITE_DEFAULT_JOURNAL_SIZE_LIMIT,
    }
    connection_options = SQLITE_CONNECTION_OPTIONS + ConnectionOptions(SQLITE_REGEXP_OPTION)
    url_has_userinfo = False

    def is_retryable(self, error: BaseException) -> bool:
        # SQLITE_BUSY and its extended codes (SQLITE_BUSY_SNAPSHOT, SQLITE_BUSY_RECOVERY, ...):
        # another connection holds the lock the statement needs - "database is locked".
        # A driver's exceptions carry SQLite's result code as ``sqlite_errorcode``, as sqlite3's do.
        error_code = getattr(error, "sqlite_errorcode", None)
        return error_code is not None and error_code & SQLITE_PRIMARY_RESULT_CODE_MASK == SQLITE_BUSY_RESULT_CODE

    def get_url_path(self, url: urlparse.ParseResult) -> str:
        path = urlparse.unquote(url.netloc + url.path)
        # `sqlite+aiosqlite:///C:/dir/db.sqlite` parses to "/C:/dir/db.sqlite", which os.remove() doesn't
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
