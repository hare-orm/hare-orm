from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.sql.terms.term import Term


class ListParameter:
    """A term binding a whole value list as one parameter - a dialect's container of a long
    ``__in``/``__not_in`` list. A statement plan binds another list of any length through it."""

    __slots__ = ()

    def get_parameter_source(self) -> Term:
        """The term whose parameter holds the list.

        Returns:
            The term.
        """
        raise NotImplementedError

    def get_parameter(self, values: list[Any], *, holds_no_term: bool = False) -> Any:
        """The parameter standing for another list, carried the way this term carries its own.

        Args:
            values: The list's encoded values, None left out.
            holds_no_term: Whether none of ``values`` is known to be a SQL term.

        Returns:
            The parameter, None when this term can't carry the list that way.
        """
        raise NotImplementedError
