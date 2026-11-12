from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.fields.field import Field
from hare.query.plans.statement.parameter_slot import ParameterSlot
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
    from hare.models import Model


@dataclass(frozen=True, slots=True)
class OpenRangeValueReference:
    """A plan value reference of ``__range`` with one bound given - the plain comparison of the bound
    (``>=`` without an upper one, ``<=`` without a lower one): its parameter slot, which side is
    open, the field converting a new bound, and the lookup's own encoder when it has one.
    Preparation holds the source wrapper; the published plan holds an immutable slot.
    """

    bound: ParameterSlot | ValueWrapper
    lower_is_open: bool
    field: Field[Any]
    value_encoder: Callable[..., list[Any]] | None = None
    #: The dialect's conversion of ``field`` - ``(value, model) -> value`` - resolved once when the plan
    #: is published (``ParameterPreparation``); None while the plan is prepared.
    convert: Callable[[Any, Any], Any] | None = None

    def bind(self, parameters: list[Any], value: Any, model: type[Model], dialect: Dialect) -> bool:
        """The parameter of a new ``(lower, upper)`` pair open on the same side.

        Args:
            parameters: The parameter buffer of this execution, filled in place.
            value: The new pair.
            model: The model queried.
            dialect: The dialect the query runs on.

        Returns:
            True when the prepared SQL shape accepts the value; False requests a fresh build.
            Conversion and validation errors propagate to the caller.
        """
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            return False
        lower, upper = value
        if (lower is None) != self.lower_is_open or (upper is None) == self.lower_is_open:
            return False
        if self.value_encoder is not None:
            encoded_lower, encoded_upper = self.value_encoder(value, model, self.field, dialect)
            bound = encoded_upper if self.lower_is_open else encoded_lower
        else:
            bound = self.convert(upper if self.lower_is_open else lower, model)  # type: ignore[misc]
        if isinstance(bound, Term):
            return False
        return self.bound.bind(parameters, bound, False)  # type: ignore[union-attr]
