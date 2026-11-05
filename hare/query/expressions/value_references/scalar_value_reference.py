from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.fields.field import Field
from hare.query.plans.statement.parameter_slot import ParameterSlot
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
    from hare.models import Model
    from hare.query.expressions.value_references.value_reference_types import ParameterValues


@dataclass(frozen=True)
class ScalarValueReference:
    """Bind a fresh filter value through the field's lookup conversion.

    Preparation identifies the criterion's ``ValueWrapper``; the published statement plan
    stores its immutable ``ParameterSlot`` instead. A cache hit uses ``get_lookup_value``
    to preserve filtering semantics rather than write-time rounding or truncation.
    """

    wrapper: ParameterSlot | ValueWrapper
    field: Field[Any]

    def get_parameter_values(self, value: Any, model: type[Model], dialect: Dialect) -> ParameterValues | None:
        """The parameter a new value gives - converted the way the filter converted the recorded
        one (``to_lookup_value()``: no write-time truncation or rounding).

        Args:
            value: The new value.
            model: The model queried.
            dialect: The dialect the query runs on.

        Returns:
            The parameters, or None when the converted value isn't a plain value (e.g. an
            IntField's cast for a float) and the query has to be built again.
        """
        lookup_value = dialect.types.get_lookup_value(self.field, value, model)
        if isinstance(lookup_value, Term):
            return None
        return [(self.wrapper, lookup_value, False)]
