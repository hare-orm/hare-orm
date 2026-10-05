from __future__ import annotations

from typing import Any

from hare.sql.terms.functions.function import Function


class JsonArray(Function):
    """A JSON array of the arguments - each dialect writes its own function."""

    requires_dialect_renderer = True

    def __init__(self, *arguments: Any, alias: str | None = None) -> None:
        super().__init__("JSON_ARRAY", *arguments, alias=alias)
