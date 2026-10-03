from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.fields.base.field import Field
from hare.sql.terms.base.term import Term
from hare.sql.terms.tuple import Tuple

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.models import Model
    from hare.query.expressions.value_refs.value_ref_types import ParameterValues


@dataclass(frozen=True)
class RowListValueRef:
    """A plan value reference of a composite key's ``pk__in`` list - the ``Tuple`` of value rows of
    ``(a, b) IN ((?, ?), ...)`` and the key's fields. A plan hit binds as many rows; the row count is
    part of the plan key.
    """

    container: Tuple
    fields: tuple[Field[Any], ...]

    def get_parameter_values(self, value: Any, model: type[Model], dialect: Dialect) -> ParameterValues | None:
        """The parameters of new key rows - each value converted by its key field, as the recorded
        rows were.

        Args:
            value: The new rows.
            model: The model queried.
            dialect: The dialect the query runs on.

        Returns:
            The parameters, or None for rows of another count or shape, a None in a row, or a value
            converting to a term.
        """
        row_terms = self.container.values
        if not isinstance(value, (list, tuple)) or len(value) != len(row_terms):
            return None
        fields = self.fields
        types = dialect.types
        parameter_values: ParameterValues = []
        row_term: Any
        for row, row_term in zip(value, row_terms, strict=True):
            if not isinstance(row, tuple) or len(row) != len(fields):
                return None
            for component, field, component_term in zip(row, fields, row_term.values, strict=True):
                if component is None:
                    return None
                lookup_value = types.get_lookup_value(field, component, model)
                if isinstance(lookup_value, Term):
                    return None
                parameter_values.append((id(component_term), lookup_value, False))
        return parameter_values
