from __future__ import annotations

from typing import Any

from hare.sql.enums import JsonValueType
from hare.sql.functions.text.collate import Collate
from hare.sql.terms.functions.function import Function


class JsonValue(Function):
    """A value of a JSON value a database builds, with the type its database writes it by. A naive
    timestamp (``is_aware=False``) is written as its wall clock."""

    requires_dialect_renderer = True

    def __init__(self, term: Any, value_type: JsonValueType, is_aware: bool = True, alias: str | None = None) -> None:
        super().__init__("JSON", Collate.strip(term), alias=alias)
        self.value_type = value_type
        self.is_aware = is_aware
