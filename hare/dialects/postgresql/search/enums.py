from __future__ import annotations

from enum import StrEnum


class TsWeight(StrEnum):
    """A Postgres tsquery/tsvector weight label."""

    A = "A"
    B = "B"
    C = "C"
    D = "D"
