from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from hare.dialects.postgresql.fields.constants import RANGE_PUBLIC_PATH

T = TypeVar("T")


@dataclass(frozen=True)
class Range(Generic[T]):
    """A range value - what every ``RangeField`` holds in Python, whatever the driver.

    Args:
        lower: The lower bound, None for unbounded.
        upper: The upper bound, None for unbounded.
        lower_inc: Whether the lower bound is included - True by default.
        upper_inc: Whether the upper bound is included - False by default.
        is_empty: Whether this is the empty range (``'empty'``), which holds no value - unlike an
            unbounded range, which has the same None bounds.
    """

    lower: T | None = None
    upper: T | None = None
    lower_inc: bool = True
    upper_inc: bool = False
    is_empty: bool = False

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        """The call building the range again - a field's default written into a migration.

        Returns:
            The class's public path, the positional arguments and the keyword arguments.
        """
        if self.is_empty:
            return RANGE_PUBLIC_PATH, [], {"is_empty": True}
        keyword_arguments: dict[str, Any] = {}
        if not self.lower_inc:
            keyword_arguments["lower_inc"] = False
        if self.upper_inc:
            keyword_arguments["upper_inc"] = True
        return RANGE_PUBLIC_PATH, [self.lower, self.upper], keyword_arguments
