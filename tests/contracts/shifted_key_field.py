from collections.abc import Callable
from typing import Any, ClassVar

from hare.fields import IntField


class ShiftedKeyField(IntField):
    """Retain original inputs while moving stored keys past the next read boundary."""

    shift: ClassVar[int] = 10000
    converted_values: ClassVar[list[int]] = []

    def get_stored_value_conversion(self, old_field: Any) -> Callable[[Any], Any]:
        def convert(value: int) -> int:
            self.converted_values.append(value)
            return value + self.shift

        return convert
