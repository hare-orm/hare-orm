from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RepeatedQueryEntry:
    """One query shape counted by a ``RepeatedQueryCollection``.

    Attributes:
        shape_key: The shape.
        count: How many queries of it ran.
        sql_sample: The SQL of the first one.
        call_site: The stack of the application code that issued the first one.
    """

    shape_key: str
    count: int
    sql_sample: str
    call_site: str
