from __future__ import annotations

#: The engine families keeping several versions of a row that ``FINAL`` merges into one - an engine
#: is of a family when its name, without a ``Replicated``/``Shared`` prefix, is one of these.
CLICKHOUSE_FINAL_ENGINES = frozenset(
    {
        "ReplacingMergeTree",
        "CollapsingMergeTree",
        "VersionedCollapsingMergeTree",
        "AggregatingMergeTree",
        "SummingMergeTree",
        "CoalescingMergeTree",
        "GraphiteMergeTree",
    }
)
#: The prefixes of a replicated or shared engine's name - ``ReplicatedReplacingMergeTree``.
CLICKHOUSE_ENGINE_PREFIXES = ("Replicated", "Shared")
#: The least rows ``sample_rows()`` takes - ``SAMPLE 1`` is the whole table, a share of one.
CLICKHOUSE_MIN_SAMPLE_ROWS = 2
#: The most rows ``sample_rows()`` and ``limit_by()`` take - ClickHouse's 64-bit row counts.
CLICKHOUSE_MAX_ROW_COUNT = 2**63 - 1
#: The range of an integer setting value - ClickHouse's 64-bit settings.
CLICKHOUSE_MIN_SETTING_INTEGER = -(2**63)
CLICKHOUSE_MAX_SETTING_INTEGER = 2**64 - 1
