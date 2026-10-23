from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from hare.dialects.base.literals.constants import SQL_NULL_BYTE, SQL_NULL_BYTE_MESSAGE
from hare.exceptions import ValidationError

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
    from hare.sql.terms.parameters.parameterizer import Parameterizer


@dataclass(frozen=True)
class SqlContext:
    """Represents the context for get_sql() methods to determine how to render SQL.

    Attributes:
        native_functions_only: Render only functions every dialect supports natively (never a
            call to a function hare registers on its own connections) - for SQL text that gets stored in DDL or a
            written migration file, which must stay portable and replayable outside hare's own
            connections.
        names_qualified_columns: Give a selected column written with its table (``"t"."c"``) its own
            name as an alias - for a database that may name such a result column by its table too.
        wraps_negated_criteria: Write ``NOT (condition)`` - for a database reading ``NOT (x) IS NULL``
            as the function call ``NOT(x)`` compared with NULL.
        names_dialect_terms: Write a term the dialect has no SQL for as its identity text
            (``TermIdentityText``) - for the text naming and comparing an index's keys on every
            database, never run.
        connection_functions: Where the names of the functions, collations and operators only hare's
            own connections have are noted as the SQL calls them - for SQL a database keeps (an
            index's keys, a constraint's condition), which can't call one; None to note none.
    """

    quote_char: str
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
    names_qualified_columns: bool = False
    wraps_negated_criteria: bool = False
    names_dialect_terms: bool = False
    connection_functions: list[str] | None = field(default=None, compare=False)

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

    def note_connection_function(self, name: str) -> str:
        """Notes a function, collation or operator the SQL calls that only hare's own connections have
        (``connection_functions``).

        Args:
            name: Its name.

        Returns:
            The name.
        """
        if self.connection_functions is not None:
            self.connection_functions.append(name)
        return name

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
        new_context = type(self).__new__(type(self))
        new_context.__dict__.update(self.__dict__)
        return new_context

    def copy(self, **kwargs: Any) -> SqlContext:
        """A copy with the fields given changed - the context itself when they change nothing.

        Args:
            kwargs: The fields and their values.

        Returns:
            The copy.
        """
        # Sets the fields on a shallow copy - a future field is carried over by itself;
        # object.__setattr__ as the class is frozen. A copy changing nothing returns self (a flat
        # AND chain asks for many). A plain loop - a generator was measurably slower.
        for key, value in kwargs.items():
            if getattr(self, key) != value:
                break
        else:
            return self
        new_context = copy.copy(self)
        for key, value in kwargs.items():
            object.__setattr__(new_context, key, value)
        return new_context
