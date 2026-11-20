from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from hare.native.native_modules import NativeModules
from hare.sql.terms.parameters.parameterizer import Parameterizer

if TYPE_CHECKING:
    from hare.query.plans.statement.declarations import ParameterBinder


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
    #: The shared executor of this immutable description; it retains no input value.
    bind: ParameterBinder = field(init=False, compare=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "bind",
            NativeModules.rows.ParameterSlot(
                self.positions,
                self.value_type,
                self.literal,
                self.value_options,
                self.value_options_getter,
                Parameterizer.should_parameterize
                if issubclass(self.value_type, Parameterizer.conditionally_parameterized_types)
                else None,
                Enum if issubclass(self.value_type, Enum) else None,
            ).bind,
        )
