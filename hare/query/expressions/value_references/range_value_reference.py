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
class RangeValueReference:
    """A plan value reference of ``__range`` with both bounds given: the two parameter slots
    of the ``BETWEEN``, the field converting a new bound, and the lookup's own encoder when it has
    one (``__year__range`` encodes ints).
    Preparation holds the source wrappers; the published plan holds immutable slots.
    """

    lower: ParameterSlot | ValueWrapper
    upper: ParameterSlot | ValueWrapper
    field: Field[Any]
    value_encoder: Callable[..., list[Any]] | None = None
    #: The dialect's conversion of ``field`` - ``(value, model) -> value`` - resolved once when the plan
    #: is published (``ParameterPreparation``); None while the plan is prepared.
    convert: Callable[[Any, Any], Any] | None = None

    def bind(self, parameters: list[Any], value: Any, model: type[Model], dialect: Dialect) -> bool:
        """The two parameters of a new ``(lower, upper)`` pair.

        Args:
            parameters: The parameter buffer of this execution, filled in place.
            value: The new pair.
            model: The model queried.
            dialect: The dialect the query runs on.

        Returns:
            True when the prepared SQL shape accepts the value; False requests a fresh build.
            Conversion and validation errors propagate to the caller.
        """
        if not isinstance(value, (list, tuple)) or len(value) != 2 or None in value:
            return False
        if self.value_encoder is not None:
            lower, upper = self.value_encoder(value, model, self.field, dialect)
        else:
            convert = self.convert
            lower, upper = (convert(bound, model) for bound in value)  # type: ignore[misc]
        if isinstance(lower, Term) or isinstance(upper, Term):
            return False
        return self.lower.bind(parameters, lower) and self.upper.bind(parameters, upper)  # type: ignore[union-attr]
