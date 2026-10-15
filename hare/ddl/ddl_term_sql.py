from __future__ import annotations

from typing import TYPE_CHECKING

from hare.exceptions import ConfigurationError, UnSupportedError
from hare.sql.exceptions import FunctionException

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
    from hare.sql.sql_context import SqlContext
    from hare.sql.terms.term import Term


class DdlTermSql:
    """The SQL of a term written into DDL - an index's key, a constraint's condition - which the
    database runs on its own, outside hare's connections: no function only they have."""

    @staticmethod
    def get_sql(term: Term, sql_context: SqlContext, model: type[Model], declared: object) -> str:
        """The term's SQL for DDL.

        Args:
            term: The term.
            sql_context: The DDL's context - ``native_functions_only`` set.
            model: The model the DDL is for.
            declared: What the term was declared as, for the error.

        Returns:
            The SQL.

        Raises:
            ConfigurationError: The dialect has no SQL of its own for the term.
            UnSupportedError: The SQL calls a function only hare's connections have.
        """
        try:
            sql = term.get_sql(sql_context)
        except FunctionException as error:
            raise ConfigurationError(f"{model.__name__}: {declared!r} can't be written into DDL - {error}") from None
        if (hare_function := sql_context.dialect.renderers.get_connection_only_function(sql)) is not None:
            raise UnSupportedError(
                f"{model.__name__}: {declared!r} needs {hare_function}, a function {sql_context.dialect} only has on "
                "hare's own connections - it can't be part of a constraint or index"
            )
        return sql
