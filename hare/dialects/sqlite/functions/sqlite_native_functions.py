from __future__ import annotations

from rust.native import sqlite_functions


class SqliteNativeFunctions:
    """The compiled ``rust.native.sqlite_functions`` module - the SQLite functions registered on a
    connection. Each runs the common inputs natively and hands the rest to its Python function."""

    #: The module.
    module = sqlite_functions
