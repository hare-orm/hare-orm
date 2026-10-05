from __future__ import annotations

#: PRAGMA values SQLite itself treats as "enabled" for a boolean pragma such as foreign_keys.
SQLITE_PRAGMA_ENABLED_VALUES = frozenset({"1", "on", "true", "yes"})

#: Lists every row whose foreign key points at a missing parent row.
SQLITE_FOREIGN_KEY_CHECK_SQL = "PRAGMA foreign_key_check"

#: The schema objects that may keep ``ALTER TABLE ... DROP COLUMN`` from dropping a column: tables
#: (a CHECK in the definition), triggers and views.
SQLITE_DROP_COLUMN_BLOCKERS_SQL = (
    "SELECT type, tbl_name, sql FROM {schema}sqlite_master WHERE type IN ('table', 'trigger', 'view')"
)

#: The keyword of a CHECK constraint in a table's ``CREATE TABLE`` text.
SQLITE_CHECK_KEYWORD = "CHECK"

#: A ``PRAGMA index_xinfo`` column number of an index over an expression instead of a column.
SQLITE_EXPRESSION_INDEX_COLUMN = -2

#: Keyword of a primary key column whose ids are never reused - SQLite keeps the highest id it
#: ever issued for such a table in ``sqlite_sequence``.
SQLITE_AUTOINCREMENT_KEYWORD = "AUTOINCREMENT"

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
#: table rebuild changes the field's type - the conversion a Postgres ``USING column::type`` does.
#: A value the conversion can't read is copied unchanged.
#: A non-zero number becomes true.
SQLITE_NUMBER_TO_BOOLEAN_SQL = (
    "CASE WHEN {column} IS NULL THEN NULL WHEN CAST({column} AS REAL) <> 0 THEN 1 ELSE 0 END"
)

#: The spellings Postgres reads as a boolean.
SQLITE_TEXT_TO_BOOLEAN_SQL = (
    "CASE WHEN lower(trim({column})) IN ('t', 'true', 'y', 'yes', 'on', '1') THEN 1 "
    "WHEN lower(trim({column})) IN ('f', 'false', 'n', 'no', 'off', '0') THEN 0 ELSE {column} END"
)

#: Postgres prints a boolean as text as ``true``/``false``.
SQLITE_BOOLEAN_TO_TEXT_SQL = "CASE WHEN {column} IS NULL THEN NULL WHEN {column} THEN 'true' ELSE 'false' END"

#: A fractional number is rounded to the nearest integer.
SQLITE_NUMBER_TO_INTEGER_SQL = "CASE WHEN {column} IS NULL THEN NULL ELSE CAST(ROUND({column}) AS INTEGER) END"

#: An integer as fixed-point decimal text with the field's number of decimal places.
SQLITE_INTEGER_TO_DECIMAL_SQL = (
    "CASE WHEN {column} IS NULL THEN NULL ELSE CAST(CAST({column} AS INTEGER) AS TEXT){fraction} END"
)

#: A floating-point number as fixed-point decimal text, rounded to the field's decimal places.
SQLITE_FLOAT_TO_DECIMAL_SQL = "CASE WHEN {column} IS NULL THEN NULL ELSE printf('%.{decimal_places}f', {column}) END"

#: The date of a stored datetime - of its UTC instant for an aware one.
SQLITE_DATETIME_TO_DATE_SQL = "COALESCE(date({column}), {column})"

#: Midnight of a stored date - as a UTC instant when time zone support is on.
SQLITE_DATE_TO_AWARE_DATETIME_SQL = "COALESCE(datetime({column}) || '+00:00', {column})"

SQLITE_DATE_TO_NAIVE_DATETIME_SQL = "COALESCE(datetime({column}), {column})"

#: How a comment is escaped inside the ``/* ... */`` SQLite keeps a table or column comment in.
SQLITE_COMMENT_ESCAPES = str.maketrans(
    {"\x00": "\\0", "\\": "\\\\", "\n": "\\n", "\r": "\\r", "\x1a": "\\Z", "/": "\\/"}
)

#: The implicit column naming a row of a table - what a backfill of a table without a primary key
#: picks its batches by.
SQLITE_ROW_IDENTITY_COLUMN = "rowid"
