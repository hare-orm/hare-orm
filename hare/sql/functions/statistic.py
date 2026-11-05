from __future__ import annotations

from hare.sql.functions.distinct_option_function import DistinctOptionFunction


class Statistic(DistinctOptionFunction):
    """A statistic aggregate (``STDDEV_POP``, ``VAR_SAMP``, ...) - the database's own, or hare's where it
    has none."""

    structure_attributes = ()
