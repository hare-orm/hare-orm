from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.fields.field import Field
from hare.query.execution.write_statements import BoundWriteValue
from hare.query.plans.statement.parameter_slot import ParameterSlot
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
    from hare.models import Model


@dataclass(frozen=True, slots=True)
class WriteValueReference:
    """A value an ``UPDATE`` assigns: the ``ValueWrapper`` of its SET clause and the field converting
    it by the write conversion (``to_db_value()``), which rounds and truncates where a comparison
    doesn't.
    """

    wrapper: ParameterSlot | ValueWrapper
    field: Field[Any]
    #: The dialect's conversion of ``field`` - ``(value, model) -> value`` - resolved once when the plan
    #: is published (``ParameterPreparation``); None while the plan is prepared.
    convert: Callable[[Any, Any], Any] | None = None

    def bind(self, parameters: list[Any], value: Any, model: type[Model], dialect: Dialect) -> bool:
        """The parameter of a new assigned value - converted the way the write converted the
        recorded one.

        Args:
            parameters: The parameter buffer of this execution, filled in place.
            value: The new value.
            model: The model queried.
            dialect: The dialect the query runs on.

        Returns:
            True when the prepared SQL shape accepts the value; False requests a fresh build.
            Conversion and validation errors propagate to the caller.
        """
        db_value = (
            value.database_value if isinstance(value, BoundWriteValue) else self.convert(value, None)  # type: ignore[misc]
        )
        if isinstance(db_value, Term):
            return False
        return self.wrapper.bind(parameters, db_value, False)  # type: ignore[union-attr]
