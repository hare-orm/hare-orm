from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.sql.terms.functions.function import Function

if TYPE_CHECKING:
    pass
from hare.sql.functions.collate import Collate


class DecimalAsText(Function):
    """A decimal rendered with exactly ``scale`` places as the same text on every dialect."""

    requires_dialect_renderer = True

    def __init__(self, term: Any, scale: int, alias: str | None = None) -> None:
        super().__init__("CAST", Collate.strip(term), alias=alias)
        self.scale = scale
