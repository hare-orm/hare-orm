from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, ClassVar

from hare.core.cache import Cache
from hare.sql.constants import EMPTY_BUILDER_CACHE_SIZE
from hare.sql.context import DEFAULT_SQL_CONTEXT, SqlContext
from hare.sql.queries.tables.selectable import Selectable
from hare.sql.queries.tables.table import Table
from hare.sql.terms.base.term import Term

if TYPE_CHECKING:
    from hare.sql.queries.builder.query_builder import QueryBuilder


class Query:
    """The entry point of hare.sql - builds queries step by step. Immutable."""

    SQL_CONTEXT: SqlContext = DEFAULT_SQL_CONTEXT
    #: (Query class,) -> its shared empty builder.
    EMPTY_BUILDERS: ClassVar[Cache[QueryBuilder]] = Cache(EMPTY_BUILDER_CACHE_SIZE)

    @classmethod
    def _builder(cls, **kwargs: Any) -> QueryBuilder:
        # Imported here: the modules import each other.
        from hare.sql.queries.builder.query_builder import QueryBuilder

        return QueryBuilder(**kwargs)

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
                ``wrapper_cls``, ``immutable``).

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
                ``wrapper_cls``, ``immutable``).

        Returns:
            QueryBuilder.
        """
        return cls._builder(**kwargs).into(table)

    @classmethod
    def with_(cls, table: str | Selectable, name: str, *terms: Term, **kwargs: Any) -> QueryBuilder:
        return cls._builder(**kwargs).with_(table, name, *terms)

    @classmethod
    def select(cls, *terms: int | float | str | bool | Term, **kwargs: Any) -> QueryBuilder:
        """Query builder entry point. Initializes query building without a table and selects fields.

        Useful when testing SQL functions.

        Args:
            terms: A list of terms to select. These can be any type of int, float, str, bool, or
                Term. They cannot be a Field unless ``Query.from_`` is called first.
            kwargs: Passed on to the ``QueryBuilder`` (``wrap_set_operation_queries``,
                ``wrapper_cls``, ``immutable``).

        Returns:
            QueryBuilder.
        """
        return cls._builder(**kwargs).select(*terms)

    @classmethod
    def update(cls, table: str | Table, **kwargs) -> QueryBuilder:
        """Query builder entry point. Initializes query building and sets the table to update.

        Makes the query an UPDATE query.

        Args:
            table: An instance of a Table object or a string table name.
            kwargs: Passed on to the ``QueryBuilder`` (``wrap_set_operation_queries``,
                ``wrapper_cls``, ``immutable``).

        Returns:
            QueryBuilder.
        """
        return cls._builder(**kwargs).update(table)

    @classmethod
    def get_insert_rows_source_sql(
        cls, table_sql: str, rows_source_sql: str, returning_columns_sql: Sequence[str]
    ) -> str:
        """An ``INSERT`` of the rows a source gives - the dialect's rows of column defaults.

        Args:
            table_sql: The table inserted into.
            rows_source_sql: The source of the rows.
            returning_columns_sql: The returned columns, none for no ``RETURNING`` clause.

        Returns:
            The statement.
        """
        sql = f"INSERT INTO {table_sql} {rows_source_sql}"  # nosec B608
        if returning_columns_sql:
            sql += " RETURNING " + ", ".join(returning_columns_sql)
        return sql

    @classmethod
    def get_typed_placeholder_template(cls, placeholder_template: str, cast_type: str | None) -> str:
        """A parameter placeholder's template, cast to a type when one is given.

        Args:
            placeholder_template: The dialect's placeholder template.
            cast_type: The type to cast the parameter to, None for none.

        Returns:
            The template.
        """
        if cast_type is None:
            return placeholder_template
        return f"CAST({placeholder_template} AS {cast_type.upper()})"

    @classmethod
    def get_values_table_columns_sql(cls, column_names: Sequence[str]) -> str:
        """The select list giving the columns of a ``VALUES`` table their names.

        Args:
            column_names: The names, in column order.

        Returns:
            The select list.
        """
        return ", ".join(f"column{index} AS {name}" for index, name in enumerate(column_names, start=1))

    @classmethod
    def get_update_from_values_sql(
        cls,
        *,
        with_sql: str,
        table_sql: str,
        assignments: Sequence[tuple[str, str]],
        values_columns_sql: str,
        values_sql: str,
        values_alias_sql: str,
        matches: Sequence[tuple[str, str]],
        condition_sql: str | None,
        returning: Sequence[tuple[str, str]],
    ) -> str:
        """An ``UPDATE`` writing each row of a ``VALUES`` table into the table row it matches.

        Args:
            with_sql: The ``WITH`` clause, or an empty string.
            table_sql: The updated table.
            assignments: ``(column, value SQL)`` per assigned column.
            values_columns_sql: The select list naming the ``VALUES`` table's columns.
            values_sql: The rows of the ``VALUES`` table.
            values_alias_sql: The alias of the ``VALUES`` table.
            matches: ``(table column SQL, VALUES column SQL)`` pairs a row is matched by.
            condition_sql: A further condition on the updated rows, or None.
            returning: ``(column SQL, alias)`` per returned column.

        Returns:
            The statement.
        """
        set_sql = ", ".join(f"{column} = {value_sql}" for column, value_sql in assignments)
        where_parts = [f"{column_sql} = {value_sql}" for column_sql, value_sql in matches]
        if condition_sql is not None:
            where_parts.append(f"({condition_sql})")
        sql = (
            f"{with_sql}UPDATE {table_sql} SET {set_sql} "  # nosec B608
            f"FROM (SELECT {values_columns_sql} FROM (VALUES {values_sql})) AS {values_alias_sql} "
            f"WHERE {' AND '.join(where_parts)}"
        )
        if returning:
            sql += " RETURNING " + ", ".join(f"{column_sql} AS {alias}" for column_sql, alias in returning)
        return sql
