from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
    from hare.models import Model
    from hare.query.expressions.value_references.value_reference_types import ValueReference


@dataclass(frozen=True, slots=True)
class RelatedValueReference:
    """The value of a filter across a relation (``related__field__lookup=value``): the reference
    recorded resolving ``field__lookup=value`` on the related model, and that model - a new value is
    converted for the model the field belongs to.
    """

    reference: ValueReference
    related_model: type[Model]

    def bind(self, parameters: list[Any], value: Any, model: type[Model], dialect: Dialect) -> bool:
        """The parameters of a new value of a filter across a relation.

        Args:
            parameters: The parameter buffer of this execution, filled in place.
            value: The new value.
            model: The model queried (not the field's own).
            dialect: The dialect the query runs on.

        Returns:
            True when the prepared SQL shape accepts the value; False requests a fresh build.
            Conversion and validation errors propagate to the caller.
        """
        return self.reference.bind(parameters, value, self.related_model, dialect)
