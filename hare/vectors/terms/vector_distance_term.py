from __future__ import annotations

from typing import Any

from hare.sql.terms.functions.function import Function
from hare.vectors.enums import VectorDistanceType


class VectorDistanceTerm(Function):
    """The distance of two vectors - each dialect writes its own SQL.

    Args:
        distance_type: What the distance measures.
        first: A vector.
        second: Another vector.
    """

    requires_dialect_renderer = True

    def __init__(self, distance_type: VectorDistanceType, first: Any, second: Any, alias: str | None = None) -> None:
        super().__init__("VECTOR_DISTANCE", first, second, alias=alias)
        self.distance_type = distance_type
