from __future__ import annotations

from pathlib import Path

#: Where the prepared SQLite versions are kept.
SQLITE_VERSIONS_DIRECTORY = Path(__file__).parent

#: sqlite.org, where every release is downloaded from.
SQLITE_DOWNLOAD_ROOT = "https://www.sqlite.org"

#: The SQLite versions of the test matrix: hare's oldest (3.35.5), each version a dialect feature
#: starts at (STRICT 3.37, ``->>`` 3.38, ``unhex()`` 3.41), Ubuntu 22.04's own (3.37.2, the 3.37 of
#: the matrix) and the newest - with their files on sqlite.org.
SQLITE_RELEASES: dict[str, dict[str, str]] = {
    "3.35.5": {
        "windows_dll": "2021/sqlite-dll-win64-x64-3350500.zip",
        "amalgamation": "2021/sqlite-amalgamation-3350500.zip",
    },
    "3.37.2": {
        "windows_dll": "2022/sqlite-dll-win64-x64-3370200.zip",
        "amalgamation": "2022/sqlite-amalgamation-3370200.zip",
    },
    "3.38.0": {
        "windows_dll": "2022/sqlite-dll-win64-x64-3380000.zip",
        "amalgamation": "2022/sqlite-amalgamation-3380000.zip",
    },
    "3.41.0": {
        "windows_dll": "2023/sqlite-dll-win64-x64-3410000.zip",
        "amalgamation": "2023/sqlite-amalgamation-3410000.zip",
    },
    "3.53.4": {
        "windows_dll": "2026/sqlite-dll-win-x64-3530400.zip",
        "amalgamation": "2026/sqlite-amalgamation-3530400.zip",
    },
}

#: The compile options of a built ``libsqlite3`` - what the official Windows DLL and the CPython
#: builds enable.
SQLITE_COMPILE_OPTIONS = (
    "-DSQLITE_ENABLE_JSON1",
    "-DSQLITE_ENABLE_FTS4",
    "-DSQLITE_ENABLE_FTS5",
    "-DSQLITE_ENABLE_RTREE",
    "-DSQLITE_ENABLE_MATH_FUNCTIONS",
    "-DSQLITE_ENABLE_DESERIALIZE",
    "-DSQLITE_ENABLE_COLUMN_METADATA",
    "-DSQLITE_THREADSAFE=1",
)

#: CPython's source release of one Python version, where its ``_sqlite3`` module is built from when
#: the Python's own one carries SQLite linked in statically - ``{release}`` is ``major.minor.micro``,
#: ``{version}`` the full version (``3.14.0rc1``).
CPYTHON_SOURCE_URL = "https://www.python.org/ftp/python/{release}/Python-{version}.tgz"
#: The directory of CPython's source release holding the ``_sqlite3`` module.
CPYTHON_SQLITE_MODULE_DIRECTORY = "Modules/_sqlite"

#: The environment variable naming the SQLite version a test run must load - the run stops when
#: the loaded one differs.
SQLITE_VERSION_ENVIRONMENT_VARIABLE = "HARE_TEST_SQLITE_VERSION"
#: The name standing for the newest SQLite version of the matrix where versions are named.
NEWEST_SQLITE_VERSION_NAME = "newest"
