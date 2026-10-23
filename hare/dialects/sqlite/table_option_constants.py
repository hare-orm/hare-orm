from __future__ import annotations

#: The table options after the column list of ``CREATE TABLE``.
SQLITE_STRICT_TABLE_OPTION = "STRICT"
SQLITE_WITHOUT_ROWID_TABLE_OPTION = "WITHOUT ROWID"

#: The type a column of each affinity is declared with in a ``STRICT`` table, which takes only
#: these - a numeric column (a date, a time) holds any value, as it does in a plain table.
SQLITE_STRICT_COLUMN_TYPES = {
    "INTEGER": "INTEGER",
    "TEXT": "TEXT",
    "BLOB": "BLOB",
    "REAL": "REAL",
    "NUMERIC": "ANY",
}
