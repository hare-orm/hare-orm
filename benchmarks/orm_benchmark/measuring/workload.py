from __future__ import annotations

from typing import Any

from orm_benchmark.constants import CLICKHOUSE_INSERT_ROWS, CLICKHOUSE_ROWS, LARGE_BULK_CREATE_FACTOR, SIZES


class Workload:
    """How much work the scenarios do for a table of ``rows`` rows."""

    @staticmethod
    def get_batch(rows: int) -> int:
        """How many rows a per-row scenario (``get()`` one by one, ``bulk_update``, ...) touches."""
        return max(5, rows // 5)

    @staticmethod
    def get_in_count(rows: int) -> int:
        """How many values the ``id IN (...)`` scenario filters by."""
        return min(rows, 200)

    @staticmethod
    def get_large_rows(rows: int) -> int:
        """How many rows the large ``bulk_create`` writes."""
        return rows * LARGE_BULK_CREATE_FACTOR

    @staticmethod
    def get_size(rows: int) -> str:
        """The ``--size`` of a widget table of ``rows`` rows."""
        return next(size for size, size_rows in SIZES.items() if size_rows == rows)

    @classmethod
    def get_counts(cls, rows: int) -> dict[str, int]:
        """The counts the scenario labels name."""
        size = cls.get_size(rows)
        return {
            "rows": rows,
            "batch": cls.get_batch(rows),
            "in_count": cls.get_in_count(rows),
            "large_rows": cls.get_large_rows(rows),
            "events": CLICKHOUSE_ROWS[size],
            "insert_rows": CLICKHOUSE_INSERT_ROWS[size],
        }

    @staticmethod
    def get_default_payload() -> dict[str, Any]:
        """The JSON document every widget starts with."""
        return {
            "tags": ["red", "blue", "green"],
            "dimensions": {"w": 10, "h": 20, "d": 5},
            "history": [{"at": index, "note": f"event-{index}"} for index in range(10)],
            "active": True,
            "notes": "a medium-sized JSON document for the json_read/json_write scenarios",
        }
