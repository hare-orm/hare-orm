from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.sql.terms.functions.function import Function

if TYPE_CHECKING:
    pass


class DatetimeAsText(Function):
    """A timestamp rendered as the text SQLite stores it in (``2020-01-02 03:04:05+00:00``, the
    fraction only when not zero; an aware value in UTC, a naive one as its wall clock) - Postgres's
    own text differs, and holds a naive value as an instant in the machine's local zone."""

    requires_dialect_renderer = True

    def __init__(self, term: Any, is_aware: bool, alias: str | None = None) -> None:
        super().__init__("CAST", term, alias=alias)
        self.is_aware = is_aware
