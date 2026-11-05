from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.query.plans.statement.parameter_slot import ParameterSlot
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
    from hare.models import Model
    from hare.query.expressions.value_references.value_reference_types import ParameterValues


@dataclass(frozen=True)
class LiteralValueReference:
    """Bind an annotation literal such as the number in ``F("price") * 1.5``.

    Preparation identifies its ``ValueWrapper``; the published plan stores a ``ParameterSlot``.
    There is no field: a cache hit binds the new value as given, or applies ``encoder`` when
    conversion is needed, such as converting a date-arithmetic ``timedelta`` to microseconds.
    """

    wrapper: ParameterSlot | ValueWrapper
    encoder: Callable[[Any], Any] | None = None

    def get_parameter_values(self, value: Any, model: type[Model], dialect: Dialect) -> ParameterValues | None:
        """The parameter of a new annotation literal - as written, or through the recorded
        encoder.

        Args:
            value: The new literal.
            model: The model queried.
            dialect: The dialect the query runs on.

        Returns:
            The parameters.
        """
        return [(self.wrapper, value if self.encoder is None else self.encoder(value), False)]
