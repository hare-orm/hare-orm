from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.models import Model
    from hare.query.expressions.value_references.value_reference_types import ParameterValues


@dataclass(frozen=True)
class LiteralValueReference:
    """A plan value reference of a literal inside an annotation (``F("price") * 1.5``): the
    ``ValueWrapper`` a ``Value`` resolves to. There is no field - a plan hit wraps the new value as
    written, or after ``encoder`` when the literal was converted (a ``timedelta`` of date arithmetic
    into microseconds).
    """

    wrapper: ValueWrapper
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
        return [(id(self.wrapper), value if self.encoder is None else self.encoder(value), False)]
