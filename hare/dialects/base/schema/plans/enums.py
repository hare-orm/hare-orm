from enum import StrEnum


class SchemaEffect(StrEnum):
    """Work a prepared schema step performs beyond changing its declarations."""

    READ_CATALOG = "read_catalog"
    LOCK_TABLES = "lock_tables"
    REWRITE_ROWS = "rewrite_rows"
    REPLACE_TABLE = "replace_table"
    CHECK_ROWS = "check_rows"
    BACKFILL_ROWS = "backfill_rows"
