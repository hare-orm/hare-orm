from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.fields.base.field import Field
from hare.sql.terms.base.term import Term
from hare.sql.terms.base.value_wrapper import ValueWrapper

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.models import Model
    from hare.query.expressions.value_refs.value_ref_types import ParameterValues


@dataclass(frozen=True)
class ScalarValueRef:
    """A single-value plan value reference (see ``ExpressionContext.value_wrapper_refs``) - the
    ORIGINAL ``ValueWrapper`` a plain ``field = value``-shaped criterion embeds, and the ``Field``
    whose ``to_db_value()`` a cache hit re-runs on a fresh raw value to build the replacement."""

    wrapper: ValueWrapper
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
        return [(id(self.wrapper), lookup_value, False)]
