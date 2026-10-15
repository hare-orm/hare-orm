from __future__ import annotations

from typing import Any

from hare.sql.sql_context import SqlContext
from hare.sql.terms.functions.function import Function


class Collate(Function):
    """`term COLLATE collation` - compares and sorts a text value by the named collation.

    SQLite carries an explicit collation on into every expression built on top of the term (a
    function call, CASE, COALESCE), not just into the comparison it's written in.
    """

    def __init__(self, term: Any, collation: str, alias: str | None = None) -> None:
        super().__init__("COLLATE", term, alias=alias)
        self.collation = collation

    def get_function_sql(self, sql_context: SqlContext) -> str:
        term_sql = self.get_arg_sql(self.args[0], sql_context)
        return f"({term_sql} COLLATE {self.collation})"

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
