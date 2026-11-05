from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hare.fields.field import Field


@dataclass(frozen=True, slots=True)
class ValueField:
    """What a column of ``.values()``/``.values_list()`` is read as.

    Attributes:
        field: The field the value is decoded through - None for a value used as the driver
            returns it.
        is_native: The value is used as the driver returns it.
    """

    field: Field[Any] | None
    is_native: bool
