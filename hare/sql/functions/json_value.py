from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.sql.enums import JsonValueType
from hare.sql.terms.functions.function import Function

if TYPE_CHECKING:
    pass
from hare.sql.functions.collate import Collate


class JsonValue(Function):
    """A value of a JSON object written on SQLite as Postgres's jsonb holds it - a boolean as
    ``true``/``false``, a Decimal as a number, a timestamp as ISO text in UTC, a JSON value nested;
    Postgres takes the value as it is. A naive timestamp (``is_aware=False``) is written as its
    wall clock without an offset."""

    requires_dialect_renderer = True

    def __init__(self, term: Any, value_type: JsonValueType, is_aware: bool = True, alias: str | None = None) -> None:
        super().__init__("JSON", Collate.strip(term), alias=alias)
        self.value_type = value_type
        self.is_aware = is_aware
