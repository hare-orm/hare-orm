from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class RepeatedQueryReport:
    """What a ``RepeatedQueryDetector`` reports once a shape's count reaches its threshold within
    the window.

    Attributes:
        shape_key: The shape.
        count: The queries of the shape in the window.
        threshold: The detector's threshold.
        window_seconds: The detector's window.
        sql_sample: The SQL of the window's first query of the shape.
        call_site: The stack of the application code that issued the query reaching the threshold.
    """

    shape_key: str
    count: int
    threshold: int
    window_seconds: float
    sql_sample: str
    call_site: str


#: A custom report handler - a metric, a structured log event, an exception in a strict test
#: environment - instead of the default warning.
ReportAction = Callable[[RepeatedQueryReport], None]
