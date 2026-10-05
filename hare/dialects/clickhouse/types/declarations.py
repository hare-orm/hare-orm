from __future__ import annotations

import dataclasses
from typing import Any


@dataclasses.dataclass(frozen=True, slots=True)
class ClickhouseTypedValue:
    """A value bound with the ClickHouse type it is written as - a value of a ``Dynamic`` or a
    ``Variant`` column, which takes the type of each value from the value itself, a container
    holding such values, whose empty value has no type of its own, and a signed integer of a container
    its literal would type as a ``UInt64``.

    Attributes:
        value: The value, as the type's column takes it; None for NULL.
        column_type: The type, named as the server names it.
        held_type: The type of the ``Dynamic`` or ``Variant`` column the value is written into - None
            for a value compared with one, and for a container.
    """

    value: Any
    column_type: str
    held_type: str | None = None
