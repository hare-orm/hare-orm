from __future__ import annotations

from typing import Any

from hare.sql.terms.tuple import Tuple


class Bracket(Tuple):
    """A term in parentheses."""

    def __init__(self, term: Any) -> None:
        super().__init__(term)
