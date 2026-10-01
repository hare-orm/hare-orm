from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.fields.base.field import Field
from hare.sql.terms.list_parameter import ListParameter

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.models import Model
    from hare.query.expressions.value_refs.value_ref_types import ParameterValues


@dataclass(frozen=True)
class ListParameterValueRef:
    """A plan value reference of an ``__in``/``__not_in`` list the dialect binds as one parameter
    (``ListParameter``): a plan binds a list of any length through it - the plan key holds no
    length for such a list.
    """

    list_parameter: ListParameter
    field: Field[Any]
    value_encoder: Callable[..., list[Any]]

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
        parameter = self.list_parameter.get_parameter(encoded_values)
        if parameter is None:
            return None
        return [(id(self.list_parameter.get_parameter_source()), parameter, False)]
