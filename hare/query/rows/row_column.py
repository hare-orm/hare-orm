from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class RowColumn:
    """A selected value's driver key and Python conversion.

    A missing converter means that the driver's value already has its declared
    representation. The converter is responsible for its field's NULL contract.
    """

    key: int | str
    reader: Callable[[Any], Any] | None = None

    def read(self, row: Any) -> Any:
        value = row[self.key]
        return value if self.reader is None else self.reader(value)
