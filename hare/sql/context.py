from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.exceptions import ValidationError
from hare.sql.constants import SQL_NULL_BYTE, SQL_NULL_BYTE_MESSAGE

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect


@dataclass(frozen=True)
class SqlContext:
    """Represents the context for get_sql() methods to determine how to render SQL.

    Attributes:
        native_functions_only: Render only functions every dialect supports natively (never a
            call to one of hare's own SQLite UDFs) - for SQL text that gets stored in DDL or a
            written migration file, which must stay portable and replayable outside hare's own
            connections.
    """

    quote_char: str
    secondary_quote_char: str
    alias_quote_char: str
    dialect: Dialect
    as_keyword: bool = False
    subquery: bool = False
    with_alias: bool = False
    with_namespace: bool = False
    subcriterion: bool = False
    parameterizer: Parameterizer | None = None
    groupby_alias: bool = True
    orderby_alias: bool = True
    native_functions_only: bool = False

    @staticmethod
    def quote_text(value: Any, quote_char: str | None) -> str:
        """Writes a name or literal into SQL text between ``quote_char``s, a ``quote_char`` inside
        it doubled.

        Args:
            value: The name or literal; written as ``str(value)``.
            quote_char: The quote character, None or empty for none.

        Returns:
            The quoted text.

        Raises:
            ValidationError: The text holds a null byte, which no SQL statement can carry.
        """
        text = str(value)
        if SQL_NULL_BYTE in text:
            # No SQL statement text can carry it - rejected before the statement is sent.
            raise ValidationError(SQL_NULL_BYTE_MESSAGE.format(text=text))
        if not quote_char:
            return text
        escaped = text.replace(quote_char, quote_char * 2)
        return f"{quote_char}{escaped}{quote_char}"

    def quote(self, name: Any) -> str:
        """Quotes a table, column, schema or constraint name with ``quote_char``.

        Args:
            name: The name.

        Returns:
            The quoted name.
        """
        return SqlContext.quote_text(name, self.quote_char)

    def quote_alias(self, alias: Any) -> str:
        """Quotes a SELECT alias with ``alias_quote_char``, else ``quote_char``.

        Args:
            alias: The alias.

        Returns:
            The quoted alias.
        """
        return SqlContext.quote_text(alias, self.alias_quote_char or self.quote_char)

    def format_alias_sql(self, sql: str, alias: str | None) -> str:
        """Appends ``AS alias`` (or just the alias, without ``as_keyword``) to a term's SQL.

        Args:
            sql: The term's SQL.
            alias: The alias, None for none.

        Returns:
            The SQL with its alias.
        """
        if alias is None:
            return sql
        as_keyword = " AS " if self.as_keyword else " "
        return f"{sql}{as_keyword}{self.quote_alias(alias)}"

    def __copy__(self) -> SqlContext:
        # A shallow __dict__ copy without copy.copy()'s generic path - this runs for every rendered
        # node.
        new_ctx = type(self).__new__(type(self))
        new_ctx.__dict__.update(self.__dict__)
        return new_ctx

    def copy(self, **kwargs) -> SqlContext:
        # Sets the fields on a shallow copy - a future field is carried over by itself;
        # object.__setattr__ as the class is frozen. A copy changing nothing returns self (a flat
        # AND chain asks for many). A plain loop - a generator was measurably slower.
        for key, value in kwargs.items():
            if getattr(self, key) != value:
                break
        else:
            return self
        new_ctx = copy.copy(self)
        for key, value in kwargs.items():
            object.__setattr__(new_ctx, key, value)
        return new_ctx


from hare.dialects.base.constants import SQL_DIALECT  # noqa: E402

#: The context a term or query renders in outside a connection (``str(term)``) - plain ISO SQL.
#: A query built for a connection renders in its dialect's own context
#: (``Query.SQL_CONTEXT`` of the dialect's query class).
DEFAULT_SQL_CONTEXT = SqlContext(
    quote_char='"',
    secondary_quote_char="'",
    alias_quote_char="",
    as_keyword=False,
    dialect=SQL_DIALECT,
)

#: Context for rendering an expression into dialect-neutral SQL text stored in an index
#: definition or a written migration file - see SqlContext.native_functions_only.
NEUTRAL_SQL_CONTEXT = DEFAULT_SQL_CONTEXT.copy(native_functions_only=True)

from hare.sql.terms.base.parameterizer import Parameterizer  # noqa: E402
