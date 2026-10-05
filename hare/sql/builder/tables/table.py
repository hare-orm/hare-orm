from __future__ import annotations

from collections.abc import Sequence
from functools import reduce
from typing import TYPE_CHECKING, Any, overload

from hare.sql.builder_methods import BuilderMethods
from hare.sql.sql_context import DEFAULT_SQL_CONTEXT, SqlContext
from hare.sql.terms.field import Field
from hare.sql.terms.term import Term

if TYPE_CHECKING:
    from hare.sql.builder.queries.query import Query
    from hare.sql.builder.queries.query_builder import QueryBuilder
    from hare.sql.builder.tables.schema import Schema
from hare.sql.builder.tables.selectable import Selectable


class Table(Selectable):
    @overload
    @staticmethod
    def _init_schema(
        schema: None,
    ) -> None: ...

    @overload
    @staticmethod
    def _init_schema(
        schema: str | list[str] | tuple[str, ...] | Schema,
    ) -> Schema: ...

    @staticmethod
    def _init_schema(
        schema: str | list[str] | tuple[str, ...] | Schema | None,
    ) -> Schema | None:
        # This is a bit complicated in order to support backwards compatibility. It should probably be cleaned up for
        # the next major release. Schema is accepted as a string, list/tuple, Schema instance, or None
        # Imported here: the modules import each other.
        from hare.sql.builder.tables.schema import Schema

        if isinstance(schema, Schema):
            return schema
        if isinstance(schema, (list, tuple)):
            return reduce(lambda obj, schema_name: Schema(schema_name, parent=obj), schema[1:], Schema(schema[0]))
        if schema is not None:
            return Schema(schema)
        return None

    def __init__(
        self,
        name: str,
        schema: Schema | str | None = None,
        alias: str | None = None,
        query_class: type[Query] | None = None,
        table_options: tuple[Any, ...] = (),
    ) -> None:
        # Imported here: the modules import each other.
        from hare.sql.builder.queries.query import Query

        super().__init__(alias)
        #: The ``Meta.table_options`` of the model whose table it is - none for a table of no model; a
        #: dialect's clauses read its own entry.
        self.table_options = table_options
        self._table_name = name
        self._schema = self._init_schema(schema)
        if query_class is None:
            query_class = Query
        elif not issubclass(query_class, Query):
            raise TypeError("Expected 'query_cls' to be subclass of Query")
        self._query_class = query_class

    def get_table_name(self) -> str:
        return self.alias or self._table_name

    def get_sql(self, sql_context: SqlContext) -> str:
        table_sql = sql_context.quote(self._table_name)

        # Without schemas, a Meta.schema model's table is created unqualified too.
        if self._schema is not None and sql_context.dialect.features.supports_schemas:
            table_sql = f"{self._schema.get_sql(sql_context)}.{table_sql}"

        return sql_context.format_alias_sql(table_sql, self.alias)

    @BuilderMethods.builder
    def replace_table(  # type:ignore[override]
        self, current_table: Table | None, new_table: Table | None
    ) -> Table | None:
        """Replaces this table with the new table if it matches the current table.

        Used when reusing tables across queries.

        Args:
            current_table: The table to be replaced.
            new_table: The table to replace with.

        Returns:
            new_table if this table is the one being replaced, otherwise a copy of this table.
        """
        if self == current_table:
            return new_table
        return None

    def __str__(self) -> str:
        return self.get_sql(DEFAULT_SQL_CONTEXT)

    def __eq__(self, other: Any) -> bool:
        return (
            isinstance(other, Table)
            and self._table_name == other._table_name
            and self._schema == other._schema
            and self.alias == other.alias
        )

    def __repr__(self) -> str:
        if self._schema:
            return f"Table('{self._table_name}', schema='{self._schema}')"
        return f"Table('{self._table_name}')"

    def __ne__(self, other: Any) -> bool:
        return not self.__eq__(other)

    def __hash__(self) -> int:
        # Cheap, attribute-based hash mirroring __eq__ exactly (table name, schema chain,
        # alias) instead of rendering full SQL just to hash it.
        schema_key = self._schema._hash_key() if self._schema else None
        return hash((self._table_name, schema_key, self.alias))

    def select(self, *terms: Sequence[int | float | str | bool | Term | Field]) -> QueryBuilder:
        """Performs a SELECT operation on the current table.

        Args:
            terms: A list of terms to select. These can be any type of int, float, str, bool,
                Term, or Field.

        Returns:
            QueryBuilder.
        """
        return self._query_class.from_(self).select(*terms)

    def update(self) -> QueryBuilder:
        """Performs an UPDATE operation on the current table.

        Returns:
            QueryBuilder.
        """
        return self._query_class.update(self)

    def insert(self, *terms: int | float | str | bool | Term | Field) -> QueryBuilder:
        """Performs an INSERT operation on the current table.

        Args:
            terms: A list of terms to select. These can be any type of int, float, str, bool,
                or any other valid data.

        Returns:
            QueryBuilder.
        """
        return self._query_class.into(self).insert(*terms)
