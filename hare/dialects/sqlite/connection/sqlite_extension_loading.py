from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from hare.exceptions import ConfigurationError


class SqliteExtensionLoading:
    """Check and scope the sqlite3 capability shared by extension loaders."""

    @staticmethod
    def raise_if_unavailable(option_name: str) -> None:
        if not hasattr(sqlite3.Connection, "enable_load_extension"):
            raise ConfigurationError(
                f"{option_name}=True needs a Python whose sqlite3 module loads extensions - this one was built "
                "without them (SQLITE_OMIT_LOAD_EXTENSION)"
            )

    @staticmethod
    @contextmanager
    def enabled(connection: sqlite3.Connection) -> Iterator[None]:
        connection.enable_load_extension(True)
        try:
            yield
        finally:
            connection.enable_load_extension(False)
