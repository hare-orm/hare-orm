from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hare.sql.terms.base.term import Term
from hare.sql.terms.base.value_wrapper import ValueWrapper


@dataclass(frozen=True, slots=True)
class UpdateParameterSlot:
    """A placeholder for the caller's ``position``-th bound value in an UPDATE rendered through a
    ``Parameterizer`` - keeps it in text order among an expression's own bound literals."""

    position: int

    @staticmethod
    def as_term(position: int) -> Term:
        """Builds the term rendering this slot's placeholder.

        Args:
            position: The 0-based index into the caller's bound values.

        Returns:
            A value wrapper the parameterizer binds in text order.
        """
        return ValueWrapper(UpdateParameterSlot(position))

    @staticmethod
    def bind(parameter_layout: list[Any], values: list[Any]) -> list[Any]:
        """Builds a statement's bound values from its layout.

        Args:
            parameter_layout: The parameterizer's values - slots and an expression's literals.
            values: The caller's plain bound values.

        Returns:
            The bound values in placeholder order.
        """
        return [
            values[parameter.position] if isinstance(parameter, UpdateParameterSlot) else parameter
            for parameter in parameter_layout
        ]
