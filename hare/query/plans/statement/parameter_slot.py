from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from hare.sql.terms.parameters.parameterizer import Parameterizer


@dataclass(frozen=True, slots=True)
class ParameterSlot:
    """The SQL positions and rendering constraints of one converted input value.

    Slots contain no SQL nodes or values from the query that prepared them. A conversion
    supplies a fresh value; a different rendering shape requests preparation again, whereas
    conversion and validation exceptions propagate to the caller.
    """

    positions: tuple[int, ...]
    value_type: type
    literal: bool = False
    value_options: Any = None
    value_options_getter: Callable[[Any], Any] | None = None

    def bind(self, parameters: list[Any], value: Any, always_parameterized: bool = False) -> bool:
        """Write a converted value to every occurrence, preserving the prepared SQL shape."""
        if self.literal:
            return False
        if not self.positions:
            return True
        if (
            type(value) is not self.value_type
            or (self.value_options_getter is not None and self.value_options_getter(value) != self.value_options)
            or (not always_parameterized and not Parameterizer.should_parameterize(value))
        ):
            return False
        while isinstance(value, Enum):
            value = value.value
        for position in self.positions:
            parameters[position] = value
        return True
