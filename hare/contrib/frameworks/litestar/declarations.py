from __future__ import annotations

from collections.abc import Callable
from typing import Any, NamedTuple


class DTOExtraField(NamedTuple):
    """A value of a ``HareDTO`` response that is no field of the model: its type, and how it is read
    from a row - ``None`` where the row has no value for it."""

    annotation: Any
    read: Callable[[object], Any]
