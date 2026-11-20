"""SQLite's own text and JSON representations of a double."""

import sqlite3
from contextlib import closing


class SqliteFloatText:
    """How the SQLite library this process links - the one aiosqlite runs on - writes a double."""

    @staticmethod
    def get(value: float) -> str:
        """The text of ``CAST(value AS TEXT)``."""
        with closing(sqlite3.connect(":memory:")) as connection:
            return connection.execute("SELECT CAST(? AS TEXT)", (value,)).fetchone()[0]

    @staticmethod
    def get_json(value: float) -> str:
        """The JSON number written by SQLite, independently of CAST's formatting."""
        with closing(sqlite3.connect(":memory:")) as connection:
            return connection.execute("SELECT json_quote(?)", (value,)).fetchone()[0]
