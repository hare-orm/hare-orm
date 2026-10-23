from __future__ import annotations

from typing import Any

from hare.sql.terms.functions.function import Function


class JsonSortKey(Function):
    """A JSON value in the form its database orders and compares it - the value itself, the value a
    database's own function reads of a path, or the typed value it reads of JSON text."""

    requires_dialect_renderer = True

    def __init__(self, term: Any, alias: str | None = None) -> None:
        super().__init__("JSON", term, alias=alias)
