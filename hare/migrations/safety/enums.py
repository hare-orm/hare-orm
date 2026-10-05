from __future__ import annotations

from enum import StrEnum


class MigrationRiskCode(StrEnum):
    """What makes a migration operation risky on a database in use - each code names one rule of the
    migration safety check. A migration lists the codes it accepts in ``safety_exemptions``."""

    #: A NOT NULL field with only a Python default: every existing row is updated in the migration.
    ADD_FIELD_BACKFILLS_ROWS = "add_field_backfills_rows"
    #: A field whose database default is a volatile expression: the table is rewritten.
    ADD_FIELD_VOLATILE_DEFAULT = "add_field_volatile_default"
    #: A field change the database makes by rewriting the table.
    ALTER_FIELD_REWRITES_TABLE = "alter_field_rewrites_table"
    #: An index built while the table's writes wait.
    ADD_INDEX_WITHOUT_CONCURRENTLY = "add_index_without_concurrently"
    #: A CHECK constraint whose existing rows are checked while the table's writes wait.
    ADD_CHECK_CONSTRAINT_VALIDATES_ROWS = "add_check_constraint_validates_rows"
    #: A foreign key whose existing rows are checked while both tables' writes wait.
    ADD_FOREIGN_KEY_VALIDATES_ROWS = "add_foreign_key_validates_rows"
    #: A unique constraint whose index is built while the table's writes wait.
    ADD_UNIQUE_CONSTRAINT_BUILDS_INDEX = "add_unique_constraint_builds_index"
    #: NOT NULL set on a column by scanning the table while it is locked.
    SET_NOT_NULL_SCANS_TABLE = "set_not_null_scans_table"
    #: A column renamed while running code still uses its old name.
    RENAME_FIELD = "rename_field"
    #: A column dropped while running code may still read it.
    REMOVE_FIELD = "remove_field"
    #: A table renamed while running code still uses its old name.
    RENAME_MODEL = "rename_model"
    #: A table dropped while running code may still read it.
    DELETE_MODEL = "delete_model"
    #: Raw SQL - the check can't tell what it does.
    RUN_SQL = "run_sql"
    #: Schema changes and Python code in one transaction - the locks are held while the code runs.
    SCHEMA_CHANGE_WITH_RUN_PYTHON = "schema_change_with_run_python"
