from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from hare.core.caching.cache import Cache
from hare.sql.builder.tables.selectable import Selectable
from hare.sql.builder.tables.table import Table
from hare.sql.constants import EMPTY_BUILDER_CACHE_SIZE
from hare.sql.sql_context import DEFAULT_SQL_CONTEXT, SqlContext
from hare.sql.terms.term import Term

if TYPE_CHECKING:
    from hare.sql.builder.queries.query_builder import QueryBuilder


class Query:
    """The entry point of hare.sql - builds queries step by step. Immutable."""

    SQL_CONTEXT: SqlContext = DEFAULT_SQL_CONTEXT
    #: (Query class,) -> its shared empty builder.
    EMPTY_BUILDERS: ClassVar[Cache[QueryBuilder]] = Cache(EMPTY_BUILDER_CACHE_SIZE)

    @classmethod
    def _builder(cls, **kwargs: Any) -> QueryBuilder:
        # Imported here: the modules import each other.
        from hare.sql.builder.queries.query_builder import QueryBuilder

        return QueryBuilder(query_class=cls, **kwargs)

    @classmethod
    def get_empty_builder(cls) -> QueryBuilder:
        """A shared empty builder of this dialect - a query's placeholder before it is built.

        Returns:
            The builder; every builder method returns a copy, so it is never changed.
        """
        builder = Query.EMPTY_BUILDERS.get((cls,))
        if builder is None:
            builder = Query.EMPTY_BUILDERS[(cls,)] = cls._builder()
        return builder

    @classmethod
    def from_(cls, table: Selectable | str, **kwargs: Any) -> QueryBuilder:
        """Query builder entry point. Initializes query building and sets the table to select from.

        Makes the query a SELECT query.

        Args:
            table: An instance of a Table object or a string table name.
            kwargs: Passed on to the ``QueryBuilder`` (``wrap_set_operation_queries``,
                ``wrapper_class``, ``immutable``).

        Returns:
            QueryBuilder.
        """
        return cls._builder(**kwargs).from_(table)

    @classmethod
    def into(cls, table: Table | str, **kwargs: Any) -> QueryBuilder:
        """Query builder entry point. Initializes query building and sets the table to insert into.

        Makes the query an INSERT query.

        Args:
            table: An instance of a Table object or a string table name.
            kwargs: Passed on to the ``QueryBuilder`` (``wrap_set_operation_queries``,
                ``wrapper_class``, ``immutable``).

        Returns:
            QueryBuilder.
        """
        return cls._builder(**kwargs).into(table)

    @classmethod
    def with_(cls, table: Selectable, name: str, *terms: Term, **kwargs: Any) -> QueryBuilder:
        return cls._builder(**kwargs).with_(table, name, *terms)

    @classmethod
    def select(cls, *terms: int | float | str | bool | Term, **kwargs: Any) -> QueryBuilder:
        """Query builder entry point. Initializes query building without a table and selects fields.

        Useful when testing SQL functions.

        Args:
            terms: A list of terms to select. These can be any type of int, float, str, bool, or
                Term. They cannot be a Field unless ``Query.from_`` is called first.
            kwargs: Passed on to the ``QueryBuilder`` (``wrap_set_operation_queries``,
                ``wrapper_class``, ``immutable``).

        Returns:
            QueryBuilder.
        """
        return cls._builder(**kwargs).select(*terms)

    @classmethod
    def update(cls, table: str | Table, **kwargs: Any) -> QueryBuilder:
        """Query builder entry point. Initializes query building and sets the table to update.

        Makes the query an UPDATE query.

        Args:
            table: An instance of a Table object or a string table name.
            kwargs: Passed on to the ``QueryBuilder`` (``wrap_set_operation_queries``,
                ``wrapper_class``, ``immutable``).

        Returns:
            QueryBuilder.
        """
        return cls._builder(**kwargs).update(table)
