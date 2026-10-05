from __future__ import annotations

from typing import Any

from hare.sql.sql_context import SqlContext
from hare.sql.terms.functions.function import Function


class Collate(Function):
    """`term COLLATE collation` - compares and sorts a text value by the named collation.

    A database may carry an explicit collation on into every expression built on top of the term (a
    function call, CASE, COALESCE), not just into the comparison it's written in.
    """

    def __init__(self, term: Any, collation: str, alias: str | None = None, *, names_collation: bool = False) -> None:
        """
        Args:
            term: The text term.
            collation: The collation - its SQL as it is, or a name the dialect writes.
            alias: The term's alias.
            names_collation: Whether ``collation`` is a name the dialect writes
                (``SqlLiterals.get_collation_sql()``) - one a user gave.
        """
        super().__init__("COLLATE", term, alias=alias)
        self.collation = collation
        self.names_collation = names_collation

    def get_function_sql(self, sql_context: SqlContext) -> str:
        term_sql = self.get_arg_sql(self.args[0], sql_context)
        collation_sql = (
            sql_context.dialect.literals.get_collation_sql(self.collation) if self.names_collation else self.collation
        )
        return f"({term_sql} COLLATE {collation_sql})"

    @classmethod
    def strip(cls, term: Any) -> Any:
        """The term without its collation.

        Args:
            term: A term, possibly a `Collate`.

        Returns:
            The collated term itself for a `Collate`, `term` unchanged otherwise.
        """
        return term.args[0] if isinstance(term, cls) else term

    @staticmethod
    def is_decimal_text(term: Any) -> bool:
        """Whether a term is a DecimalField column's text under an exact-decimal collation.

        Args:
            term: A term.

        Returns:
            True for a `DecimalTextCollate`.
        """
        # Imported here: the modules import each other.
        from hare.sql.functions.text.decimal_text_collate import DecimalTextCollate

        return isinstance(term, DecimalTextCollate)
