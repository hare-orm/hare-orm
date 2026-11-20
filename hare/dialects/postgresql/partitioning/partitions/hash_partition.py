from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from hare.dialects.postgresql.partitioning.partitions.partition import Partition


@dataclass(frozen=True)
class HashPartition(Partition):
    """One remainder of a PostgreSQL hash partitioning."""

    modulus: int
    remainder: int

    def get_bound_sql(self, render_value: Callable[[int, Any], str]) -> str:
        return f"FOR VALUES WITH (MODULUS {self.modulus}, REMAINDER {self.remainder})"
