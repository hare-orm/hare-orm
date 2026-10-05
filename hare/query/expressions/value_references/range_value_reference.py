from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.fields.field import Field
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.models import Model
    from hare.query.expressions.value_references.value_reference_types import ParameterValues


@dataclass(frozen=True)
class RangeValueReference:
    """A plan value reference of ``__range`` with both bounds given: the two ``ValueWrapper`` objects
    of the ``BETWEEN``, the field converting a new bound, and the lookup's own encoder when it has
    one (``__year__range`` encodes ints).
    """

    lower: ValueWrapper
    upper: ValueWrapper
    field: Field[Any]
    value_encoder: Callable[..., list[Any]] | None = None

    def get_parameter_values(self, value: Any, model: type[Model], dialect: Dialect) -> ParameterValues | None:
        """The two parameters of a new ``(lower, upper)`` pair.

        Args:
            value: The new pair.
            model: The model queried.
            dialect: The dialect the query runs on.

        Returns:
            The parameters, or None for anything but a two-item pair without None, or a bound
            that converts to a term.
        """
        if not isinstance(value, (list, tuple)) or len(value) != 2 or None in value:
            return None
        if self.value_encoder is not None:
            lower, upper = self.value_encoder(value, model, self.field, dialect)
        else:
            lower, upper = (dialect.types.get_lookup_value(self.field, bound, model) for bound in value)
        if isinstance(lower, Term) or isinstance(upper, Term):
            return None
        return [(id(self.lower), lower, False), (id(self.upper), upper, False)]
