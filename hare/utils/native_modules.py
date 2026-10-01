from __future__ import annotations


class NativeModules:
    """The parts of hare's compiled extension ``rust.native`` the Python code speeds itself up with -
    None for a part where the extension isn't built, and the Python code does the same work there.
    """

    #: ``rust.native.rows`` - reads and writes rows, values and JSON text.
    try:
        from rust.native import rows
    except ImportError:  # pragma: nocoverage
        rows = None

    #: ``rust.native.sqlite_functions`` - the SQLite functions and collations run per row.
    try:
        from rust.native import sqlite_functions
    except ImportError:  # pragma: nocoverage
        sqlite_functions = None
