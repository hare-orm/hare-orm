from __future__ import annotations

from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from typing import Self

    from hare.sql.queries.tables.table import Table
    from hare.sql.terms.field import Field
from hare.sql.terms.criteria.criterion import Criterion

T = TypeVar("T")


class EmptyCriterion(Criterion):
    """No condition yet: combining it with a criterion gives that criterion."""

    is_aggregate: bool | None = None
    tables_: set[Table] = set()

    def fields_(self) -> set[Field]:
        return set()

    def __and__(self, other: T) -> T:
        return other

    def __or__(self, other: T) -> T:
        return other

    def __xor__(self, other: T) -> T:
        return other

    def __invert__(self) -> Self:  # type: ignore[override]
        # No condition negated is still no condition.
        return self

    def __bool__(self) -> bool:
        return False
