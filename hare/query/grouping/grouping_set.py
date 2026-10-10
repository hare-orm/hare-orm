from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, ClassVar

from hare.exceptions import QueryError

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.sql.terms.grouping.grouping_element import GroupingElement
    from hare.sql.terms.term import Term


class GroupingSet:
    """Several groupings of one query - ``values(...).annotate(...).group_by(Rollup("region", "city"))``:
    the rows of each grouping one after another, a field a grouping leaves out NULL in its rows.
    ``Grouping(field)`` tells such a NULL from a NULL value.

    Args:
        fields: The grouped field or annotation names.

    Raises:
        QueryError: A name isn't a non-empty string, or none is given.
    """

    element_class: ClassVar[type[GroupingElement]]

    def __init__(self, *fields: str) -> None:
        if not fields or not all(isinstance(field, str) and field for field in fields):
            raise QueryError(f"{type(self).__name__}() takes field names, got {fields!r}")
        self.fields = fields

    @property
    def field_names(self) -> tuple[str, ...]:
        """Every name the groupings read, once each, in order."""
        return tuple(dict.fromkeys(self.fields))

    def get_element(self, terms_by_name: Mapping[str, list[Term]]) -> Term:
        """The ``GROUP BY`` element of the groupings.

        Args:
            terms_by_name: The grouped terms of each name - a composite key has several.

        Returns:
            The element.
        """
        return self.element_class(*(term for name in self.fields for term in terms_by_name[name]))

    def get_plan_key(self) -> tuple[Any, ...]:
        """What tells the groupings apart in a query plan's key."""
        return (type(self), self.fields)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({', '.join(map(repr, self.fields))})"
