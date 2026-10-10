from __future__ import annotations

from typing import Any

from hare.sql.terms.functions.function import Function


# Null Functions
class Coalesce(Function):
    def __init__(self, term: Any, *default_values: Any, **kwargs: Any) -> None:
        super().__init__("COALESCE", term, *default_values, **kwargs)
