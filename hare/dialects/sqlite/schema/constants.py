from __future__ import annotations

import re

from hare.lazy_loading.lazy_pattern import LazyPattern

#: PRAGMA values SQLite itself treats as "enabled" for a boolean pragma such as foreign_keys.
SQLITE_PRAGMA_ENABLED_VALUES = frozenset({"1", "on", "true", "yes"})

#: Lists every row whose foreign key points at a missing parent row.
SQLITE_FOREIGN_KEY_CHECK_SQL = "PRAGMA foreign_key_check"
#: Deletes one row of a table by its rowid.
SQLITE_DELETE_ROW_SQL = "DELETE FROM {table} WHERE rowid = ?"  # nosec B608


#: A generated primary key - an INTEGER alias of the rowid, never reusing an id: AUTOINCREMENT follows
#: its PRIMARY KEY.
SQLITE_GENERATED_PK_TEMPLATE = "{field_name} {generated_sql}{primary_key} AUTOINCREMENT NOT NULL{comment}"

#: Carries a rebuilt table's highest issued id over from the table it replaces: raises the
#: counter the copied rows gave the new table, or creates it when no row was copied.
SQLITE_RAISE_REBUILT_TABLE_SEQUENCE_SQL = (
    "UPDATE sqlite_sequence SET seq = (SELECT MAX(seq) FROM sqlite_sequence WHERE name = {old_table}) "
    "WHERE name = {new_table} AND seq < (SELECT MAX(seq) FROM sqlite_sequence WHERE name = {old_table})"
)

#: The highest id is read in a subquery: a HAVING without GROUP BY needs SQLite 3.39.
SQLITE_COPY_REBUILT_TABLE_SEQUENCE_SQL = (
    "INSERT INTO sqlite_sequence (name, seq) SELECT {new_table}, seq FROM "
    "(SELECT MAX(seq) AS seq FROM sqlite_sequence WHERE name = {old_table}) "
    "WHERE seq IS NOT NULL AND NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name = {new_table})"
)

#: Renaming a rebuilt table into place with the legacy behavior switched on: the modern rename
#: re-validates every view and trigger of the schema, and one naming the table being replaced
#: fails while that table is briefly missing.
SQLITE_ENABLE_LEGACY_ALTER_TABLE_SQL = "PRAGMA legacy_alter_table = ON"

SQLITE_DISABLE_LEGACY_ALTER_TABLE_SQL = "PRAGMA legacy_alter_table = OFF"

#: Expressions converting a column's stored value to the storage format of the new field when a
#: table rebuild changes the field's type. A value the conversion can't read is refused before the
#: rebuild.
#: A non-zero number becomes true.
SQLITE_NUMBER_TO_BOOLEAN_SQL = (
    "CASE WHEN {column} IS NULL THEN NULL WHEN CAST({column} AS REAL) <> 0 THEN 1 ELSE 0 END"
)

#: The texts of a boolean - as ``Cast()`` reads them.
SQLITE_TEXT_TO_BOOLEAN_SQL = (
    "CASE WHEN lower(trim({column})) IN ('true', '1') THEN 1 "
    "WHEN lower(trim({column})) IN ('false', '0') THEN 0 ELSE {column} END"
)

#: A fractional number loses its fraction, as SQLite's ``CAST`` cuts it off.
SQLITE_NUMBER_TO_INTEGER_SQL = "CASE WHEN {column} IS NULL THEN NULL ELSE CAST({column} AS INTEGER) END"

#: The date of a stored datetime - of its UTC instant for an aware one.
SQLITE_DATETIME_TO_DATE_SQL = "COALESCE(date({column}), {column})"

#: Midnight of a stored date - as a UTC instant when time zone support is on.
SQLITE_DATE_TO_AWARE_DATETIME_SQL = "COALESCE(datetime({column}) || '+00:00', {column})"

SQLITE_DATE_TO_NAIVE_DATETIME_SQL = "COALESCE(datetime({column}), {column})"

#: How a comment is escaped inside the ``/* ... */`` SQLite keeps a table or column comment in.
SQLITE_COMMENT_ESCAPES = str.maketrans(
    {"\x00": "\\0", "\\": "\\\\", "\n": "\\n", "\r": "\\r", "\x1a": "\\Z", "/": "\\/"}
)
#: The character each escape of a comment stands for - by the character after its backslash.
SQLITE_COMMENT_UNESCAPES = {"0": "\x00", "\\": "\\", "n": "\n", "r": "\r", "Z": "\x1a", "/": "/"}

#: The implicit column naming a row of a table - what a backfill of a table without a primary key
#: picks its batches by.
SQLITE_ROW_IDENTITY_COLUMN = "rowid"

#: A column made a date - the date of a datetime or of text a date reads.
SQLITE_TEXT_TO_DATE_SQL = "COALESCE(date({column}), {column})"

#: The DEFAULT ``ALTER TABLE ... ADD COLUMN`` takes on a table holding rows: a constant - a signed number, a
#: literal, NULL, TRUE or FALSE; no expression, no CURRENT_TIMESTAMP.
SQLITE_ADDED_COLUMN_DEFAULT_PATTERN = LazyPattern(
    r"\s*(?:[+-]?\s*(?:(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?|0x[0-9a-f]+)|'(?:[^']|'')*'|x'[0-9a-f]*'"
    r"|null|true|false)\s*",
    re.IGNORECASE,
)

#: Text an integer column takes: an optional sign and digits.
SQLITE_INTEGER_TEXT_PATTERN = r"\s*[+-]?\d+\s*"

#: Text a number column takes - a number literal; one Python reads but SQLite keeps as text (``nan``, ``inf``,
#: ``1_000``) isn't one.
SQLITE_NUMBER_TEXT_PATTERN = r"\s*[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\s*"

#: The texts of a boolean - those ``SQLITE_TEXT_TO_BOOLEAN_SQL`` converts.
SQLITE_BOOLEAN_TEXT_SPELLINGS = frozenset({"true", "1", "false", "0"})

#: A stored value a type change can't read.
SQLITE_INVALID_INPUT_MESSAGE = 'the column holds "{value}", which is no {new_type}'
#: A stored value the changed field reads but refuses - a geometry of another type or SRID, a vector of another length.
SQLITE_UNFIT_VALUE_MESSAGE = "the column holds a value the changed field refuses - {refusal}"

#: The SQL functions reading text as a date, a datetime and a time - NULL for text they can't read.
SQLITE_TEMPORAL_READER_FUNCTIONS = {"date": "date", "timestamp": "datetime", "time": "time"}
