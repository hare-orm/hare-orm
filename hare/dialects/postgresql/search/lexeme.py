from __future__ import annotations

from hare.dialects.postgresql.search.enums import TsWeight
from hare.dialects.postgresql.search.lexeme_combinable import LexemeCombinable
from hare.exceptions import QueryError
from hare.query.expressions import Expression, ExpressionContext, ExpressionResult


class Lexeme(LexemeCombinable, Expression):
    """A single raw tsquery lexeme (e.g. ``'cat':A*``), for building a `SearchQuery` with
    `search_type="raw"` by combining `Lexeme` instances with ``|``/``&``/``~``.

    Args:
        value: The lexeme text.
        invert: Negate this lexeme (``!``).
        prefix: Match as a prefix (``:*``).
        weight: A `TsWeight` to require on matching lexemes.

    Raises:
        ValueError: If `value` is empty, or `weight` is not a recognized weight letter.
        TypeError: If `value` is not a string.
    """

    def __init__(
        self,
        value: str,
        invert: bool = False,
        prefix: bool = False,
        weight: TsWeight | str | None = None,
    ) -> None:
        if value == "":
            raise QueryError("Lexeme value cannot be empty.")
        if not isinstance(value, str):
            raise TypeError(f"Lexeme value must be a string, got {value.__class__.__name__}.")
        self.value = value
        self.invert = invert
        self.prefix = prefix
        self.weight = TsWeight(weight.upper()) if weight is not None else None

    def _as_tsquery(self) -> str:
        # Inside a quoted tsquery lexeme a backslash escapes the next character, and a quote is
        # doubled.
        token = "'" + self.value.replace("\\", "\\\\").replace("'", "''") + "'"
        label = ""
        if self.prefix:
            label += "*"
        if self.weight:
            label += self.weight
        if label:
            token = f"{token}:{label}"
        if self.invert:
            token = f"!{token}"
        return token

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        return self.get_tsquery_result(expression_context)

    def __invert__(self) -> Lexeme:
        return Lexeme(
            self.value,
            invert=not self.invert,
            prefix=self.prefix,
            weight=self.weight,
        )
