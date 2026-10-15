from __future__ import annotations

from collections.abc import Callable
from typing import Any

from hare.sql.terms.parameters.parameterizer import Parameterizer
from hare.sql.terms.term import Term


class RecordingParameterizer(Parameterizer):
    """A ``Parameterizer`` that also keeps, for every parameter, the term it came from, and the
    terms written into the text as literals - what a compiled statement needs to bind other
    values into the same SQL text."""

    def __init__(self, placeholder_factory: Callable[[int], str] | None = None) -> None:
        super().__init__(placeholder_factory)
        #: The term each parameter came from, parallel to ``values``.
        self.sources: list[Term | None] = []
        #: The terms whose values the text holds as literals.
        self.literal_sources: list[Term] = []

    def _add_value(self, value: Any, source: Term | None) -> None:
        self.sources.append(source)
        super()._add_value(value, source)

    def record_literal(self, source: Term) -> None:
        self.literal_sources.append(source)
