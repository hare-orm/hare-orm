from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from hare.fields.data.temporal.temporal_values import TemporalValues

if TYPE_CHECKING:
    # One signature whichever branch below runs - ciso8601 (the accel extra, Linux/macOS only)
    # isn't installed on every machine mypy runs on, and its presence would change the inferred
    # type from one platform to another.
    def parse_datetime(value: str) -> datetime.datetime: ...
else:
    try:
        from ciso8601 import parse_datetime
    except ImportError:  # pragma: nocoverage
        parse_datetime = TemporalValues.parse_iso_datetime
