from __future__ import annotations

from enum import StrEnum


class ClickhouseDialectName(StrEnum):
    """The name ClickHouse's dialect is registered under."""

    CLICKHOUSE = "clickhouse"


class ClickhouseClause(StrEnum):
    """A clause ClickHouse's QuerySet methods set on a query (``QueryBuilder._dialect_clauses``)."""

    #: ``FINAL`` after the model's table - its rows merged as the engine merges them.
    FINAL = "final"
    #: ``SAMPLE <rows>`` - about that many rows.
    SAMPLE_ROWS = "sample_rows"
    #: ``OFFSET <fraction>`` of a ``SAMPLE`` - the part of the sampled key range read.
    SAMPLE_OFFSET = "sample_offset"
    #: ``PREWHERE <condition>`` - read before the other columns.
    PREWHERE = "prewhere"
    #: ``LIMIT <n> [OFFSET <m>] BY <expressions>`` - rows per group of values.
    LIMIT_BY = "limit_by"
    #: ``SETTINGS <name> = <value>, ...`` of the statement.
    SETTINGS = "settings"


class ClickhouseLookup(StrEnum):
    """A lookup ClickHouse adds to every value."""

    #: ``GLOBAL IN`` - the list or subquery read once, on the server the query was sent to, and sent to
    #: every shard of a ``Distributed`` table.
    GLOBAL_IN = "global_in"
    #: ``GLOBAL NOT IN``.
    GLOBAL_NOT_IN = "global_not_in"
