from __future__ import annotations

from typing import Any

from hare.sql.terms.functions.function import Function


class BooleanAsText(Function):
    """A boolean term rendered as ``'true'``/``'false'`` text on every dialect (NULL stays NULL).

    SQLite has no boolean type and would otherwise render a stored boolean as ``1``/``0``.
    """

    requires_dialect_renderer = True

    def __init__(self, term: Any, alias: str | None = None) -> None:
        super().__init__("CAST", term, alias=alias)
