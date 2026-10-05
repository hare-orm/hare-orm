"""The frames of a window function: ``RowRange`` and ``ValueRange``."""

from __future__ import annotations

from hare.query.expressions.frames.declarations import RowRange, ValueRange
from hare.query.expressions.frames.window_frame import WindowFrame

__all__ = [
    "RowRange",
    "ValueRange",
    "WindowFrame",
]
