from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.fields.field import Field
from hare.query.filters.lookups.value_encoders import ValueEncoders
from hare.sql.terms.parameters.list_parameter import ListParameter

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.models import Model
    from hare.query.expressions.value_references.value_reference_types import ParameterValues


@dataclass(frozen=True)
class ListParameterValueReference:
    """A plan value reference of an ``__in``/``__not_in`` list the dialect binds as one parameter
    (``ListParameter``): a plan binds a list of any length through it - the plan key holds no
    length for such a list.
    """

    list_parameter: ListParameter
    field: Field[Any]
    value_encoder: Callable[..., list[Any]]
    #: The id of the term whose parameter holds the list.
    parameter_source_id: int = dataclasses.field(init=False)
    #: Whether the values are encoded as ``ValueEncoders.encode_list()`` encodes them.
    encodes_plain_list: bool = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameter_source_id", id(self.list_parameter.get_parameter_source()))
        object.__setattr__(self, "encodes_plain_list", self.value_encoder is ValueEncoders.encode_list)

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
        if self.encodes_plain_list:
            encoded_values, holds_no_term = ValueEncoders.encode_bound_list(value, model, self.field, dialect)
        else:
            encoded_values = [
                encoded_value
                for encoded_value in self.value_encoder(value, model, self.field, dialect)
                if encoded_value is not None
            ]
            holds_no_term = False
        parameter = self.list_parameter.get_parameter(encoded_values, holds_no_term=holds_no_term)
        if parameter is None:
            return None
        return [(self.parameter_source_id, parameter, False)]
