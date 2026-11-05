from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hare.sql.terms.term import Term


class ListParameter:
    """A term binding a whole value list as one parameter - a dialect's container of a long
    ``__in``/``__not_in`` list. A statement plan binds another list of any length through it."""

    __slots__ = ()

    def get_parameter_encoder(self) -> Callable[[list[Any]], Any]:
        """Return a converter retaining only type/shape settings, never this SQL term or its values."""
        raise NotImplementedError

    def get_parameter_source(self) -> Term:
        """The term whose parameter holds the list.

        Returns:
            The term.
        """
        raise NotImplementedError
