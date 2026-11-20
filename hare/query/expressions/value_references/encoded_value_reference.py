from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from hare.fields.field import Field
from hare.query.plans.statement.parameter_slot import ParameterSlot
from hare.sql.terms.array import Array
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
    from hare.models import Model


@dataclass(frozen=True, slots=True)
class EncodedValueReference:
    """One encoded lookup parameter, held by a scalar wrapper or a bound array.

    A plan hit runs the encoder on the new value. ``item_key`` selects the scalar
    comparison value of a JSON filter whose path and operator belong to the plan key.
    """

    wrapper: ParameterSlot | ValueWrapper | Array
    field: Field[Any]
    value_encoder: Callable[..., Any]
    item_key: str | None = None

    def bind(self, parameters: list[Any], value: Any, model: type[Model], dialect: Dialect) -> bool:
        """The parameter of a new value - through the recorded encoder.

        Args:
            parameters: The parameter buffer of this execution, filled in place.
            value: The new value.
            model: The model queried.
            dialect: The dialect the query runs on.

        Returns:
            True when the prepared SQL shape accepts the value; False requests a fresh build.
            Conversion and validation errors propagate to the caller.
        """
        encoded_value = self.value_encoder(value, model, self.field, dialect)
        if self.item_key is not None:
            encoded_value = encoded_value[self.item_key]
            if isinstance(encoded_value, (int, float, Decimal)):
                encoded_value = dialect.parameters.get_bindable_number(encoded_value)
        if isinstance(encoded_value, Term):
            return False
        return self.wrapper.bind(parameters, encoded_value, False)  # type: ignore[union-attr]
