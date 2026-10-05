from __future__ import annotations


class SqliteNativeFunctions:
    """The compiled ``rust.native.sqlite_functions`` module - the SQLite functions and collations
    whose Python versions ran per row, registered in their place when it is built."""

    #: The module - None where it isn't built.
    try:
        from rust.native import sqlite_functions as module
    except ImportError:  # pragma: nocoverage
        module = None
