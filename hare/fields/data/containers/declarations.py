from __future__ import annotations

from typing import Any


class TupleValue(tuple[Any, ...]):
    """A tuple bound for a tuple column - a dialect writes it as its tuple, not as a list."""

    __slots__ = ()


class MapValue(dict[Any, Any]):
    """A map bound for a map column - a dialect writes it as its map, not as JSON."""
