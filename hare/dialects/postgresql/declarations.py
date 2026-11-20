from __future__ import annotations

from dataclasses import dataclass

from hare.dialects.base.features import Features


@dataclass(frozen=True, slots=True)
class PostgresqlFeatures(Features):
    """What PostgreSQL and its driver support beyond what every dialect is asked - read by PostgreSQL's own
    parts.

    Attributes:
        explain_options: The server's supported ``EXPLAIN`` options, upper-case.
        supports_partitioned_exclusion_constraints: A partitioned table takes an exclusion constraint.
    """

    supports_partitioned_exclusion_constraints: bool = False
    explain_options: frozenset[str] = frozenset()
