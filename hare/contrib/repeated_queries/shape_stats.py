from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ShapeStats:
    """The count of one query shape in the current window of a ``RepeatedQueryDetector``.

    Attributes:
        sql_sample: The SQL of the window's first query.
        window_start: ``time.monotonic()`` at that query.
        count: The queries of the shape in the window.
    """

    sql_sample: str
    window_start: float
    count: int = 0
