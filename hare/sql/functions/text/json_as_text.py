from __future__ import annotations

from typing import Any

from hare.sql.terms.functions.function import Function


class JsonAsText(Function):
    """A JSON value rendered as the text PostgreSQL's ``jsonb`` writes of it on every dialect - a space
    after each ``,`` and ``:`` (NULL stays NULL)."""

    requires_dialect_renderer = True

    def __init__(self, term: Any, alias: str | None = None) -> None:
        super().__init__("CAST", term, alias=alias)
