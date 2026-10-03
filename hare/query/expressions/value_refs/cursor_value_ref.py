from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.fields.base.field import Field
from hare.sql.terms.base.value_wrapper import ValueWrapper

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.models import Model
    from hare.query.expressions.value_refs.value_ref_types import ParameterValues


@dataclass(frozen=True)
class CursorValueRef:
    """A plan value reference of one keyset boundary value. The criterion holds the value twice - in
    the comparison and in the equality the later fields' branches reuse - as two ``ValueWrapper``
    objects; ``equality_wrapper`` is None for the last bounded field.
    """

    comparison_wrapper: ValueWrapper
    equality_wrapper: ValueWrapper | None
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
        db_value = dialect.types.get_db_value(self.field, value, model)
        if self.equality_wrapper is None:
            return [(id(self.comparison_wrapper), db_value, False)]
        return [(id(self.comparison_wrapper), db_value, False), (id(self.equality_wrapper), db_value, False)]
