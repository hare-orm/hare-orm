from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hare.fields.field import Field
from hare.query.expressions.expression import Expression
from hare.query.expressions.value import Value
from hare.sql.terms.term import Term


@dataclass(frozen=True)
class ResultBranch:
    """One value a CASE/COALESCE result can be picked from.

    Attributes:
        value: A bare literal (a ``Value`` unwrapped to what it holds), or the expression.
        field: The field an expression's value is decoded through, None for a literal or an
            expression of unknown type.
    """

    value: Any
    field: Field[Any] | None
    is_literal: bool

    @classmethod
    def of(cls, value: Any, field: Field[Any] | None) -> ResultBranch:
        """A branch for a CASE/COALESCE argument given as a literal or an expression.

        Args:
            value: The argument as given.
            field: The field its resolved value is decoded through.

        Returns:
            The branch - a ``Value`` counts as the literal it wraps.
        """
        if isinstance(value, Value):
            return cls(value.value, None, is_literal=True)
        if isinstance(value, (Expression, Term)):
            return cls(value, field, is_literal=False)
        return cls(value, None, is_literal=True)

    @property
    def is_typed(self) -> bool:
        """Whether the branch has a known type - a None literal or an untyped expression hasn't."""
        return self.value is not None if self.is_literal else self.field is not None
