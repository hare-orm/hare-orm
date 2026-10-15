from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.models import Model
    from hare.query.expressions.value_references.value_reference_types import ParameterValues, ValueReference


@dataclass(frozen=True)
class RelatedValueReference:
    """The value of a filter across a relation (``related__field__lookup=value``): the reference
    recorded resolving ``field__lookup=value`` on the related model, and that model - a new value is
    converted for the model the field belongs to.
    """

    reference: ValueReference
    related_model: type[Model]

    def get_parameter_values(self, value: Any, model: type[Model], dialect: Dialect) -> ParameterValues | None:
        """The parameters of a new value of a filter across a relation.

        Args:
            value: The new value.
            model: The model queried (not the field's own).
            dialect: The dialect the query runs on.

        Returns:
            The parameters the related model's filter gives, or None.
        """
        return self.reference.get_parameter_values(value, self.related_model, dialect)
