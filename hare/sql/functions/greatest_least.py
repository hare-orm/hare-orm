from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.sql.context import SqlContext
from hare.sql.terms.functions.function import Function

if TYPE_CHECKING:
    pass


class GreatestLeast(Function):
    """``GREATEST``/``LEAST`` - Postgres's own, a UDF skipping NULLs on SQLite."""

    def __init__(self, name: str, *args: Any, compares_numbers: bool = False, alias: str | None = None) -> None:
        """
        Args:
            name: ``GREATEST`` or ``LEAST``.
            args: The values.
            compares_numbers: The values are numbers - compared numerically on SQLite.
            alias: Optional alias for the term.
        """
        super().__init__(name, *args, alias=alias)
        self.compares_numbers = compares_numbers

    def get_function_sql(self, ctx: SqlContext) -> str:
        return f"{self.name}({', '.join(self.get_arg_sql(arg, ctx) for arg in self.args)})"
