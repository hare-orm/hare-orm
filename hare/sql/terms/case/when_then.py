from __future__ import annotations

from hare.sql.terms.term import Term


class WhenThen(Term):
    """This is not a real term, but a helper to store the when and then terms."""

    def __init__(self, when: Term, then: Term) -> None:
        self.when = when
        self.then = then
