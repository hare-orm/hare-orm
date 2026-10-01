from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.ddl.conditions import ConstraintCondition
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.utils.class_path import ClassPath

if TYPE_CHECKING:
    from hare.query.expressions import Q


@dataclass(frozen=True)
class CheckConstraint:
    """A named ``CHECK`` constraint.

    Args:
        check: The condition every row has to meet - a ``Q`` over the model's own fields, or
            ``RawSQLTerm`` of a raw SQL predicate.
        name: The constraint's name.

    Raises:
        ConfigurationError: The condition is neither a ``Q`` nor a ``RawSQLTerm``.
    """

    check: Q | RawSQLTerm
    name: str

    def __post_init__(self) -> None:
        ConstraintCondition.raise_if_not_condition(self.check, "CheckConstraint.check")

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        path = ClassPath.get(self.__class__)
        return path, [], {"check": self.check, "name": self.name}
