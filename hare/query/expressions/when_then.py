from __future__ import annotations

from typing import TYPE_CHECKING

from hare.sql.terms.base.term import Term

if TYPE_CHECKING:  # pragma: nocoverage
    pass


class WhenThen(Term):
    """This is not a real term, but a helper to store the when and then terms."""

    def __init__(self, when: Term, then: Term) -> None:
        self.when = when
        self.then = then
