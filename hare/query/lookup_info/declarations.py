from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.field import Field
    from hare.models import Model


@dataclass(frozen=True, slots=True)
class LookupKeyPosition:
    """Where the reading of a ``.filter()`` key stands.

    Attributes:
        model: The model the key starts at.
        key: The filter key.
        label: What the key is named as in an error.
        owner_model: The model of the segment being read.
        relations: The relations crossed to reach it.
    """

    model: type[Model]
    key: str
    label: str
    owner_model: type[Model]
    relations: tuple[Field[Any], ...]
