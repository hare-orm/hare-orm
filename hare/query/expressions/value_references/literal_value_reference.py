from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.query.plans.statement.parameter_slot import ParameterSlot
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
    from hare.models import Model


@dataclass(frozen=True, slots=True)
class LiteralValueReference:
    """Bind an annotation literal such as the number in ``F("price") * 1.5``.

    Preparation identifies its ``ValueWrapper``; the published plan stores a ``ParameterSlot``.
    There is no field: a cache hit binds the new value as given, or applies ``encoder`` when
    conversion is needed, such as converting a date-arithmetic ``timedelta`` to microseconds.
    """

    wrapper: ParameterSlot | ValueWrapper
    encoder: Callable[[Any], Any] | None = None

    def bind(self, parameters: list[Any], value: Any, model: type[Model], dialect: Dialect) -> bool:
        """The parameter of a new annotation literal - as written, or through the recorded
        encoder.

        Args:
            parameters: The parameter buffer of this execution, filled in place.
            value: The new literal.
            model: The model queried.
            dialect: The dialect the query runs on.

        Returns:
            True when the prepared SQL shape accepts the value; False requests a fresh build.
            Conversion and validation errors propagate to the caller.
        """
        return self.wrapper.bind(parameters, value if self.encoder is None else self.encoder(value), False)  # type: ignore[union-attr]
