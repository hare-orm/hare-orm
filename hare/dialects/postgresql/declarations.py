from __future__ import annotations

from dataclasses import dataclass

from hare.dialects.base.features import Features


@dataclass(frozen=True, slots=True)
class PostgresqlFeatures(Features):
    """What PostgreSQL and its driver support beyond what every dialect is asked - read by PostgreSQL's own
    parts.

    Attributes:
        supports_partitioned_exclusion_constraints: A partitioned table takes an exclusion constraint.
    """

    supports_partitioned_exclusion_constraints: bool = False
