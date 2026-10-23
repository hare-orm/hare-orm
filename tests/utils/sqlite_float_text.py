"""The text the SQLite library of this process writes for a double - 15 significant digits before
SQLite 3.53, 17 from 3.53 on."""

import sqlite3
from contextlib import closing


class SqliteFloatText:
    """How the SQLite library this process links - the one aiosqlite runs on - writes a double."""

    @staticmethod
    def get(value: float) -> str:
        """The text of ``CAST(value AS TEXT)``."""
        with closing(sqlite3.connect(":memory:")) as connection:
            return connection.execute("SELECT CAST(? AS TEXT)", (value,)).fetchone()[0]

    @classmethod
    def keeps_every_digit(cls) -> bool:
        """Whether the text reads back as the same double - a double needs 17 significant digits."""
        return float(cls.get(0.1 + 0.2)) == 0.1 + 0.2
