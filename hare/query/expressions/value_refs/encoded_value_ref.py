from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.fields.base.field import Field
from hare.sql.terms.base.value_wrapper import ValueWrapper

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.models import Model
    from hare.query.expressions.value_refs.value_ref_types import ParameterValues


@dataclass(frozen=True)
class EncodedValueRef:
    """A plan value reference of a lookup that converts its value with a ``value_encoder`` and compares
    it as a plain ``ValueWrapper`` (an array's ``__len``). A plan hit runs the encoder on the new
    value.
    """

    wrapper: ValueWrapper
    field: Field[Any]
    value_encoder: Callable[..., Any]

    def get_parameter_values(self, value: Any, model: type[Model], dialect: Dialect) -> ParameterValues | None:
        """The parameter of a new value - through the recorded encoder.

        Args:
            value: The new value.
            model: The model queried.
            dialect: The dialect the query runs on.

        Returns:
            The parameters.
        """
        return [(id(self.wrapper), self.value_encoder(value, model, self.field, dialect), False)]
