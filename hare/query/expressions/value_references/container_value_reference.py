from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.query.plans.statement.parameter_slot import ParameterSlot
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
    from hare.fields.field import Field
    from hare.models import Model


@dataclass(frozen=True, slots=True)
class ContainerValueReference:
    """Bind the parameter of a typed container literal through the field's value conversion.

    The renderer keeps the container type in SQL. A later execution only converts
    the new Python value; it does not rebuild a cast or inspect function arguments.
    """

    wrapper: ParameterSlot | ValueWrapper
    field: Field[Any]
    #: The dialect's conversion of ``field`` - ``(value, model) -> value`` - resolved once when the plan
    #: is published (``ParameterPreparation``); None while the plan is prepared.
    convert: Callable[[Any, Any], Any] | None = None

    def bind(self, parameters: list[Any], value: Any, model: type[Model], dialect: Dialect) -> bool:
        converted = self.convert(value, model)  # type: ignore[misc]
        return converted is not None and self.wrapper.bind(parameters, converted)  # type: ignore[union-attr]
