from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.classes.class_path import ClassPath
from hare.ddl.conditions.constraint_condition import ConstraintCondition
from hare.ddl.conditions.enum_members_condition import EnumMembersCondition
from hare.ddl.conditions.exclusive_arc_condition import ExclusiveArcCondition
from hare.ddl.declared_names import DeclaredNames
from hare.ddl.raw_sql_term import RawSQLTerm

if TYPE_CHECKING:
    from hare.query.expressions import Q


@dataclass(frozen=True)
class CheckConstraint:
    """A named ``CHECK`` constraint.

    Args:
        check: The condition every row has to meet - a ``Q`` over the model's own fields,
            ``RawSQLTerm`` of a raw SQL predicate, an ``ExclusiveArcCondition`` or an ``EnumMembersCondition``.
        name: The constraint's name.

    Raises:
        ConfigurationError: The condition is none of them, or the name isn't a non-empty text.
    """

    check: Q | RawSQLTerm | ExclusiveArcCondition | EnumMembersCondition
    name: str

    def __post_init__(self) -> None:
        DeclaredNames.raise_if_not_name(self.name, "CheckConstraint.name")
        ConstraintCondition.raise_if_not_condition(self.check, "CheckConstraint.check")

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        path = ClassPath.get(self.__class__)
        return path, [], {"check": self.check, "name": self.name}
