from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.fields.base.field import Field
from hare.sql.terms.array import Array

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.models import Model
    from hare.query.expressions.value_refs.value_ref_types import ParameterValues


@dataclass(frozen=True)
class ArrayValueRef:
    """A plan value reference of an array ``__contains``/``__contained_by``/``__overlap``: the
    ``Array`` node of the criterion. A plan binds the whole list as the node's one array parameter,
    not a value per element.
    """

    array: Array
    field: Field[Any]
    value_encoder: Callable[..., Any]

    def get_parameter_values(self, value: Any, model: type[Model], dialect: Dialect) -> ParameterValues | None:
        """The array parameter of a new list - one parameter for the whole list.

        Args:
            value: The new list.
            model: The model queried.
            dialect: The dialect the query runs on.

        Returns:
            The parameters, or None for a value that isn't a list.
        """
        if not isinstance(value, (list, tuple, set)):
            return None
        array = self.value_encoder(value, model, self.field, dialect).args[0]
        return [(id(self.array), array.original_value, False)]
