from __future__ import annotations

from typing import Any

from hare.sql.terms.functions.function import Function


class DatetimeAsText(Function):
    """A timestamp as the text its database writes of it - a naive one (``is_aware=False``), which a
    database may hold as the instant of its wall clock in the machine's local zone, at that zone."""

    requires_dialect_renderer = True

    def __init__(self, term: Any, is_aware: bool, alias: str | None = None) -> None:
        super().__init__("CAST", term, alias=alias)
        self.is_aware = is_aware
