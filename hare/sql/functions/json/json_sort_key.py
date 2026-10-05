from __future__ import annotations

from typing import Any

from hare.sql.terms.functions.function import Function


class JsonSortKey(Function):
    """A JSON value ordered as Postgres orders ``jsonb`` - on SQLite its JSON text under a collation
    comparing JSON values, on Postgres the value itself."""

    requires_dialect_renderer = True

    def __init__(self, term: Any, alias: str | None = None) -> None:
        super().__init__("JSON", term, alias=alias)
