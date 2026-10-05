from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.classes.class_path import ClassPath
from hare.ddl.conditions.constraint_condition import ConstraintCondition
from hare.ddl.conditions.exclusive_arc_condition import ExclusiveArcCondition
from hare.ddl.raw_sql_term import RawSQLTerm

if TYPE_CHECKING:
    from hare.query.expressions import Q


@dataclass(frozen=True)
class CheckConstraint:
    """A named ``CHECK`` constraint.

    Args:
        check: The condition every row has to meet - a ``Q`` over the model's own fields,
            ``RawSQLTerm`` of a raw SQL predicate, or an ``ExclusiveArcCondition``.
        name: The constraint's name.

    Raises:
        ConfigurationError: The condition is none of them.
    """

    check: Q | RawSQLTerm | ExclusiveArcCondition
    name: str

    def __post_init__(self) -> None:
        ConstraintCondition.raise_if_not_condition(self.check, "CheckConstraint.check")

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        path = ClassPath.get(self.__class__)
        return path, [], {"check": self.check, "name": self.name}
