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
class ScalarValueReference:
    """Bind a fresh filter value through the field's lookup conversion.

    Preparation identifies the criterion's ``ValueWrapper``; the published statement plan
    stores its immutable ``ParameterSlot`` and the dialect's lookup conversion of the field
    (``TypeRegistry.get_lookup_writer()``) instead - filtering semantics rather than write-time
    rounding or truncation.
    """

    wrapper: ParameterSlot | ValueWrapper
    field: Field[Any]
    #: The dialect's conversion of ``field`` - ``(value, model) -> value`` - resolved once when the plan
    #: is published (``ParameterPreparation``); None while the plan is prepared.
    convert: Callable[[Any, Any], Any] | None = None

    def bind(self, parameters: list[Any], value: Any, model: type[Model], dialect: Dialect) -> bool:
        """The parameter a new value gives - converted the way the filter converted the recorded
        one (``to_lookup_value()``: no write-time truncation or rounding).

        Args:
            parameters: The parameter buffer of this execution, filled in place.
            value: The new value.
            model: The model queried.
            dialect: The dialect the query runs on.

        Returns:
            True when the prepared SQL shape accepts the value; False requests a fresh build.
            Conversion and validation errors propagate to the caller.
        """
        lookup_value = self.convert(value, model)  # type: ignore[misc]
        if isinstance(lookup_value, Term):
            return False
        return self.wrapper.bind(parameters, lookup_value, False)  # type: ignore[union-attr]
