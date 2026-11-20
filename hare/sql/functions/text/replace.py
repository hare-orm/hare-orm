from __future__ import annotations

from typing import Any

from hare.sql.terms.functions.function import Function


class Replace(Function):
    """``REPLACE(text, from, to)`` - every ``from`` in ``text`` replaced by ``to``."""

    structure_attributes = ()
    has_repeatable_value = True

    def __init__(self, term: Any, old: Any, new: Any, alias: str | None = None) -> None:
        super().__init__("REPLACE", term, old, new, alias=alias)
