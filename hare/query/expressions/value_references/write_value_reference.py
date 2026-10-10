from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.fields.field import Field
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.models import Model
    from hare.query.expressions.value_references.value_reference_types import ParameterValues


@dataclass(frozen=True)
class WriteValueReference:
    """A value an ``UPDATE`` assigns: the ``ValueWrapper`` of its SET clause and the field converting
    it by the write conversion (``to_db_value()``), which rounds and truncates where a comparison
    doesn't.
    """

    wrapper: ValueWrapper
    field: Field[Any]

    def get_parameter_values(self, value: Any, model: type[Model], dialect: Dialect) -> ParameterValues | None:
        """The parameter of a new assigned value - converted the way the write converted the
        recorded one.

        Args:
            value: The new value.
            model: The model queried.
            dialect: The dialect the query runs on.

        Returns:
            The parameters, or None when the converted value isn't a plain value.
        """
        db_value = dialect.types.get_db_value(self.field, value, None)
        if isinstance(db_value, Term):
            return None
        return [(id(self.wrapper), db_value, False)]
