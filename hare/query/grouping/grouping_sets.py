from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from hare.exceptions import QueryError
from hare.query.grouping.grouping_set import GroupingSet
from hare.sql.terms.grouping.grouping_sets_element import GroupingSetsElement

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.sql.terms.term import Term


class GroupingSets(GroupingSet):
    """``GROUPING SETS``: each argument one grouping - a field name, a sequence of them, or ``()`` for
    all the rows together: ``GroupingSets(("region", "city"), "product", ())``.

    Args:
        sets: The groupings.

    Raises:
        QueryError: A grouping isn't a field name or a sequence of them, or no grouping names a
            field.
    """

    def __init__(self, *sets: str | Sequence[str]) -> None:
        if not sets:
            raise QueryError("GroupingSets() takes the groupings, got none")
        groupings: list[tuple[str, ...]] = []
        for grouping in sets:
            names: tuple[str, ...] | None = None
            if isinstance(grouping, str):
                names = (grouping,)
            elif isinstance(grouping, Sequence):
                names = tuple(grouping)
            if names is None or not all(isinstance(name, str) and name for name in names):
                raise QueryError(f"GroupingSets() takes field names or sequences of them, got {grouping!r}")
            groupings.append(names)
        self.sets = tuple(groupings)
        self.fields = tuple(name for grouping in groupings for name in grouping)
        if not self.fields:
            raise QueryError(
                "GroupingSets() needs a field in one of its groupings - all the rows alone is aggregate()"
            )

    def get_element(self, terms_by_name: Mapping[str, list[Term]]) -> Term:
        return GroupingSetsElement(
            [[term for name in grouping for term in terms_by_name[name]] for grouping in self.sets]
        )

    def get_plan_key(self) -> tuple[Any, ...]:
        return (GroupingSets, self.sets)

    def __repr__(self) -> str:
        return f"GroupingSets({', '.join(map(repr, self.sets))})"
