from __future__ import annotations

from hare.sql.terms.values.literal_value import LiteralValue


class NullValue(LiteralValue):
    def __init__(self, alias: str | None = None) -> None:
        super().__init__("NULL", alias)
