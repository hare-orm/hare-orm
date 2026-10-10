from __future__ import annotations

from typing import ClassVar

from hare.query.expressions.enums import WindowFrameUnit
from hare.query.expressions.frames.window_frame import WindowFrame


class RowRange(WindowFrame):
    """A frame of rows - ``ROWS BETWEEN ...``: ``RowRange(start=-2, end=0)`` is the two rows before
    the current one and the current one."""

    frame_type: ClassVar[WindowFrameUnit] = WindowFrameUnit.ROWS


class ValueRange(WindowFrame):
    """A frame of ordering values - ``RANGE BETWEEN ...``: ``ValueRange(start=-10, end=0)`` is every
    row whose ordering value is at most 10 below the current row's, the current row's peers
    included; an offset needs the window ordered by exactly one numeric field."""

    frame_type: ClassVar[WindowFrameUnit] = WindowFrameUnit.RANGE
    offsets_need_one_ordering: ClassVar[bool] = True
