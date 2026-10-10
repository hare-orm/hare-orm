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
class OpenRangeValueReference:
    """A plan value reference of ``__range`` with one bound given - the plain comparison of the bound
    (``>=`` without an upper one, ``<=`` without a lower one): its ``ValueWrapper``, which side is
    open, the field converting a new bound, and the lookup's own encoder when it has one.
    """

    bound: ValueWrapper
    lower_is_open: bool
    field: Field[Any]
    value_encoder: Callable[..., list[Any]] | None = None

    def get_parameter_values(self, value: Any, model: type[Model], dialect: Dialect) -> ParameterValues | None:
        """The parameter of a new ``(lower, upper)`` pair open on the same side.

        Args:
            value: The new pair.
            model: The model queried.
            dialect: The dialect the query runs on.

        Returns:
            The parameter, or None for anything but a two-item pair open on this side alone, or a
            bound that converts to a term.
        """
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            return None
        lower, upper = value
        if (lower is None) != self.lower_is_open or (upper is None) == self.lower_is_open:
            return None
        if self.value_encoder is not None:
            encoded_lower, encoded_upper = self.value_encoder(value, model, self.field, dialect)
            bound = encoded_upper if self.lower_is_open else encoded_lower
        else:
            bound = dialect.types.get_lookup_value(self.field, upper if self.lower_is_open else lower, model)
        if isinstance(bound, Term):
            return None
        return [(id(self.bound), bound, False)]
