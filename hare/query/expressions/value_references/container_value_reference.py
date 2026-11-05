from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.query.plans.statement.parameter_slot import ParameterSlot
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
    from hare.fields.field import Field
    from hare.models import Model
    from hare.query.expressions.value_references.value_reference_types import ParameterValues


@dataclass(frozen=True, slots=True)
class ContainerValueReference:
    """Bind the parameter of a typed container literal through the field's value conversion.

    The renderer keeps the container type in SQL. A later execution only converts
    the new Python value; it does not rebuild a cast or inspect function arguments.
    """

    wrapper: ParameterSlot | ValueWrapper
    field: Field[Any]

    def get_parameter_values(self, value: Any, model: type[Model], dialect: Dialect) -> ParameterValues | None:
        converted = dialect.types.get_db_value(self.field, value, model)
        return None if converted is None else [(self.wrapper, converted, False)]
