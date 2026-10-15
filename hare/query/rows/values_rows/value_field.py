from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.field import Field
    from hare.models import Model


@dataclass(frozen=True, slots=True)
class ValueField:
    """What a column of ``.values()``/``.values_list()`` is read as.

    Attributes:
        model: The model of a model field, None for an annotation or a related object.
        name: The field's or annotation's name.
        field: The field the value is decoded through - None for a value used as the driver
            returns it.
        is_native: The value is used as the driver returns it.
    """

    model: type[Model] | None
    name: str
    field: Field[Any] | None
    is_native: bool
