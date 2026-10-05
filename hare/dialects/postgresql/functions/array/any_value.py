from __future__ import annotations

from typing import Any

from hare.sql.terms.functions.function import Function


class AnyValue(Function):
    """``ANY(term)`` - PostgreSQL's array membership, the right operand of ``=``/``<>``
    (``col = ANY($1::int[])``) in place of ``col IN ($1,$2,...)`` for a large value list."""

    def __init__(self, term: Any, alias: str | None = None) -> None:
        super().__init__("ANY", term, alias=alias)
