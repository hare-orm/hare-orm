from __future__ import annotations

#: SQLite's column type affinity rules, checked in order against the declared type's upper-cased
#: text: the first rule one of whose substrings the type contains names its affinity. A type no
#: rule matches has NUMERIC affinity, an empty one BLOB.
SQLITE_TYPE_AFFINITY_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("INT",), "INTEGER"),
    (("CHAR", "CLOB", "TEXT"), "TEXT"),
    (("BLOB",), "BLOB"),
    (("REAL", "FLOA", "DOUB"), "REAL"),  # codespell:ignore
)

SQLITE_DEFAULT_TYPE_AFFINITY = "NUMERIC"

SQLITE_EMPTY_TYPE_AFFINITY = "BLOB"
