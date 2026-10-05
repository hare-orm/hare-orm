from __future__ import annotations

from typing import Any

from hare.sql.functions.statistic import Statistic


# Arithmetic Functions
class StdDev(Statistic):
    """The sample standard deviation - Postgres's ``STDDEV``."""

    def __init__(self, term: Any, alias: str | None = None) -> None:
        super().__init__("STDDEV_SAMP", term, alias=alias)
