from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.fields.field import Field
from hare.query.plans.statement.parameter_slot import ParameterSlot
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
    from hare.models import Model
    from hare.query.expressions.value_references.value_reference_types import ParameterValues


@dataclass(frozen=True)
class CursorValueReference:
    """One encoded cursor value and its explicit parameter source.

    The same source can occur in the comparison and several equality branches.
    Parameter preparation records all positions in a single slot.
    """

    wrapper: ParameterSlot | ValueWrapper
    field: Field[Any]

    def get_parameter_values(self, value: Any, model: type[Model], dialect: Dialect) -> ParameterValues | None:
        """The parameters of a new keyset boundary value - the comparison and, unless the field is
        the last one bounded, the equality the criterion holds it in.

        Args:
            value: The new boundary value.
            model: The model queried.
            dialect: The dialect the query runs on.

        Returns:
            The parameters.
        """
        db_value = dialect.types.get_read_column_value(self.field, value, model)
        return [(self.wrapper, db_value, False)]
