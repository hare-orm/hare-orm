from __future__ import annotations

from typing import Any

from hare.sql.terms.functions.function import Function


class Lower(Function):
    def __init__(self, term: Any, alias: str | None = None) -> None:
        super().__init__("LOWER", term, alias=alias)
