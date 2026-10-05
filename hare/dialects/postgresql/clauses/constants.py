from __future__ import annotations

from hare.dialects.base.clauses.enums import MergeMatch, RowLockStrength

#: The output formats of ``EXPLAIN (FORMAT ...)``.
POSTGRESQL_EXPLAIN_FORMATS = frozenset({"TEXT", "JSON", "XML", "YAML"})
#: The boolean options of ``EXPLAIN (...)`` of the newest server - an older one lacks some
#: (``POSTGRESQL_EXPLAIN_OPTION_SERVER_VERSIONS``).
POSTGRESQL_EXPLAIN_OPTIONS = frozenset(
    {
        "ANALYZE",
        "BUFFERS",
        "COSTS",
        "GENERIC_PLAN",
        "MEMORY",
        "SETTINGS",
        "SERIALIZE",
        "SUMMARY",
        "TIMING",
        "VERBOSE",
        "WAL",
    }
)
#: The default options of ``EXPLAIN (...)`` when none are given.
POSTGRESQL_DEFAULT_EXPLAIN_OPTIONS = {"verbose": True}
#: A column of a written row before the write, in its ``RETURNING``.
POSTGRESQL_OLD_ROW_VALUE_TEMPLATE = "old.{column}"
#: The statement a ``COPY`` load is reported under to the observers - its rows are in no SQL text.
POSTGRESQL_COPY_STATEMENT_TEMPLATE = "COPY {table} ({columns}) FROM STDIN"
#: The default output format of ``EXPLAIN``.
POSTGRESQL_DEFAULT_EXPLAIN_FORMAT = "JSON"
#: The row lock of each strength, with its leading space.
POSTGRESQL_ROW_LOCK_SQL = {
    RowLockStrength.UPDATE: " FOR UPDATE",
    RowLockStrength.NO_KEY_UPDATE: " FOR NO KEY UPDATE",
    RowLockStrength.SHARE: " FOR SHARE",
    RowLockStrength.KEY_SHARE: " FOR KEY SHARE",
}
#: The rows each ``WHEN`` branch of a ``MERGE`` takes, as written.
POSTGRESQL_MERGE_MATCH_SQL = {
    MergeMatch.MATCHED: "MATCHED",
    MergeMatch.NOT_MATCHED: "NOT MATCHED",
    MergeMatch.NOT_MATCHED_BY_SOURCE: "NOT MATCHED BY SOURCE",
}
