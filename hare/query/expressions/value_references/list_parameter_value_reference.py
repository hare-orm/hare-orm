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
    from hare.query.expressions.value_references.value_reference_types import ParameterValues
    from hare.sql.terms.term import Term


@dataclass(frozen=True)
class ListParameterValueReference:
    """A plan value reference of an ``__in``/``__not_in`` list the dialect binds as one parameter
    (``ListParameter``): a plan binds a list of any length through it - the plan key holds no
    length for such a list. Publication replaces the temporary ``ListParameter`` source with
    its immutable parameter slot and retains the encoder required by the dialect.
    """

    list_parameter: ListParameter | None
    field: Field[Any]
    value_encoder: Callable[..., list[Any]]
    parameter_slot: ParameterSlot | None = None
    parameter_encoder: Callable[[list[Any]], Any] | None = None

    def get_parameter_values(self, value: Any, model: type[Model], dialect: Dialect) -> ParameterValues | None:
        """The parameter of a new list - encoded the way the recorded one was, all of it in one
        parameter.

        Args:
            value: The new list.
            model: The model queried.
            dialect: The dialect the query runs on.

        Returns:
            The parameter, or None for a value that isn't a list, or a list the parameter can't
            carry the way it carries the recorded one.
        """
        if not isinstance(value, (list, tuple, set)):
            return None
        # A None in the list is an IS NULL test the plan's text holds - only the other values bind.
        encoded_values = [
            encoded_value
            for encoded_value in self.value_encoder(value, model, self.field, dialect)
            if encoded_value is not None
        ]
        if self.parameter_encoder is not None:
            parameter = self.parameter_encoder(encoded_values)
            target: ParameterSlot | Term | None = self.parameter_slot
        else:
            # An unpublished reference retains its source until preparation replaces it with a slot.
            assert self.list_parameter is not None  # nosec B101
            parameter = self.list_parameter.get_parameter_encoder()(encoded_values)
            target = self.list_parameter.get_parameter_source()
        if parameter is None:
            return None
        # Both reference states provide a target; this is not input validation.
        assert target is not None  # nosec B101
        return [(target, parameter, False)]
