from __future__ import annotations

from hare.sql.terms.functions.function import Function
from hare.sql.terms.term import Term


class Length(Function):
    """The length of text in characters or of a binary value in bytes."""

    structure_attributes = ("counts_bytes",)

    def __init__(self, term: Term, *, counts_bytes: bool) -> None:
        super().__init__("LENGTH", term)
        self.counts_bytes = counts_bytes
