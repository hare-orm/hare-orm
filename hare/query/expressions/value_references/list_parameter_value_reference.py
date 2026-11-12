from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.fields.field import Field
from hare.query.plans.statement.parameter_slot import ParameterSlot
from hare.sql.terms.parameters.list_parameter import ListParameter

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
    from hare.models import Model


@dataclass(frozen=True, slots=True)
class ListParameterValueReference:
    """A plan value reference of an ``__in``/``__not_in`` list the dialect binds as one parameter
    (``ListParameter``): a plan binds a list of any length through it - the plan key holds no
    length for such a list. Publication replaces the temporary ``ListParameter`` source with
    its immutable parameter slot and retains the encoder required by the dialect. Only
    published references, with both a slot and an encoder, are bound.
    """

    list_parameter: ListParameter | None
    field: Field[Any]
    value_encoder: Callable[..., list[Any]]
    parameter_slot: ParameterSlot | None = None
    parameter_encoder: Callable[[list[Any]], Any] | None = None

    def bind(self, parameters: list[Any], value: Any, model: type[Model], dialect: Dialect) -> bool:
        """The parameter of a new list - encoded the way the recorded one was, all of it in one
        parameter.

        Args:
            parameters: The parameter buffer of this execution, filled in place.
            value: The new list.
            model: The model queried.
            dialect: The dialect the query runs on.

        Returns:
            True when the prepared SQL shape accepts the value; False requests a fresh build.
            Conversion and validation errors propagate to the caller.
        """
        if not isinstance(value, (list, tuple, set)):
            return False
        # A None in the list is an IS NULL test the plan's text holds - only the other values bind.
        encoded_values = self.value_encoder(value, model, self.field, dialect)
        if any(element is None for element in encoded_values):
            encoded_values = [element for element in encoded_values if element is not None]
        parameter = self.parameter_encoder(encoded_values)  # type: ignore[misc]
        if parameter is None:
            return False
        return self.parameter_slot.bind(parameters, parameter, False)  # type: ignore[union-attr]
