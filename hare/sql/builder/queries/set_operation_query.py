from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from hare.sql.builder.queries.query_sql_rendering import QuerySqlRendering
from hare.sql.builder.tables.cte import Cte
from hare.sql.builder.tables.selectable import Selectable
from hare.sql.builder_methods import BuilderMethods
from hare.sql.constants import DEFAULT_SQL_CONTEXT
from hare.sql.enums import SetOperation
from hare.sql.exceptions import SetOperationException
from hare.sql.order import Order
from hare.sql.sql_context import SqlContext
from hare.sql.terms.field import Field
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:
    from typing import Self
from hare.sql.builder.queries.pagination_sql import PaginationSql
from hare.sql.builder.queries.query_builder import QueryBuilder


class SetOperationQuery(PaginationSql, Selectable, Term):  # type:ignore[misc]
    """
    A query of a set operation - UNION (DISTINCT or ALL), INTERSECT or EXCEPT.

    Created by `QueryBuilder.union()`, `union_all()`, `intersect()` and `except_of()`.

    This class should not be instantiated directly.
    """

    def __init__(
        self,
        base_query: QueryBuilder,
        set_operation_query: QueryBuilder,
        set_operation: SetOperation,
        alias: str | None = None,
        wrapper_class: type[ValueWrapper] = ValueWrapper,
    ) -> None:
        super().__init__(alias)
        self.base_query = base_query
        self._set_operation = [(set_operation, set_operation_query)]
        self._orderbys: list[tuple[Term, Order | None]] = []

        self._limit: ValueWrapper | None = None
        self._offset: ValueWrapper | None = None

        self._wrapper_class = wrapper_class
        # CTEs of the whole set operation - a branch's own WITH would land after UNION.
        self._with: list[Cte] = []

    def get_read_columns(self) -> set[tuple[str, str]]:
        """The columns every query of the set operation reads, by the tables they belong to.

        Returns:
            ``(table, column)`` of each column.
        """
        # Imported here: the modules import each other.
        from hare.sql.builder.queries.query_builder import QueryBuilder

        terms: list[Any] = [
            self.base_query,
            *(query for _operation, query in self._set_operation),
            *(cte.query for cte in self._with),
            *(term for term, _order in self._orderbys),
        ]
        return QueryBuilder.get_read_columns_of_terms(terms, self)

    def __copy__(self) -> Self:
        # The lists are copied, or every operation would change them for every branch built from one
        # base.
        newone = type(self).__new__(type(self))
        newone.__dict__.update(self.__dict__)
        newone._set_operation = list(self._set_operation)
        newone._orderbys = list(self._orderbys)
        newone._with = list(self._with)
        return newone

    @BuilderMethods.builder
    def orderby(self, *fields: Field | str, **kwargs: Any) -> Self:
        """Orders the combined rows - a name by the column of the first query's table.

        Args:
            fields: The terms or column names.
            kwargs: ``order``, the direction.

        Returns:
            The query.
        """
        for field in fields:
            order_term = (
                Field(field, table=self.base_query._from[0])
                if isinstance(field, str)
                else self.base_query.wrap_constant(field)
            )

            self._orderbys.append((order_term, kwargs.get("order")))
        return self

    @BuilderMethods.builder
    def limit(self, limit: int) -> Self:
        self._limit = cast("ValueWrapper", self.wrap_constant(limit))
        return self

    @BuilderMethods.builder
    def offset(self, offset: int) -> Self:
        self._offset = cast("ValueWrapper", self.wrap_constant(offset))
        return self

    @BuilderMethods.builder
    def union(self, other: Selectable) -> Self:
        self._set_operation.append((SetOperation.UNION, other))  # type:ignore[arg-type]
        return self

    @BuilderMethods.builder
    def union_all(self, other: Selectable) -> Self:
        self._set_operation.append((SetOperation.UNION_ALL, other))  # type:ignore[arg-type]
        return self

    @BuilderMethods.builder
    def intersect(self, other: Selectable) -> Self:
        self._set_operation.append((SetOperation.INTERSECT, other))  # type:ignore[arg-type]
        return self

    @BuilderMethods.builder
    def except_of(self, other: Selectable) -> Self:
        self._set_operation.append((SetOperation.EXCEPT_OF, other))  # type:ignore[arg-type]
        return self

    def __add__(self, other: Selectable) -> Self:  # type:ignore[override]
        return self.union(other)

    def __mul__(self, other: Selectable) -> Self:  # type:ignore[override]
        return self.union_all(other)

    def __str__(self) -> str:
        return self.get_sql(DEFAULT_SQL_CONTEXT)

    def get_sql(self, sql_context: SqlContext) -> str:
        set_operation_template = " {type} {query_string}"

        # The base query's dialect and quote character.
        sql_context = sql_context.copy(
            dialect=self.base_query.query_class.SQL_CONTEXT.dialect,
            quote_char=self.base_query.query_class.SQL_CONTEXT.quote_char,
            parameterizer=sql_context.parameterizer,
        )
        set_context = sql_context.copy(subquery=self.base_query.wrap_set_operation_queries)

        # Rendered once, at the front, before the branches - its parameters come first, as its text
        # does.
        with_prefix = QuerySqlRendering.with_sql(self._with, sql_context) if self._with else ""
        base_querystring = self.base_query.get_sql(set_context)
        querystring = with_prefix + base_querystring
        for set_operation, set_operation_query in self._set_operation:
            set_operation_querystring = set_operation_query.get_sql(set_context)

            if len(self.base_query._selects) != len(set_operation_query._selects):
                raise SetOperationException(
                    "Queries must have an equal number of select statements in a set operation."
                    f"\n\nMain Query:\n{base_querystring}\n\nSet Operations Query:\n{set_operation_querystring}"
                )

            querystring += set_operation_template.format(
                type=sql_context.dialect.clauses.get_set_operation_sql(set_operation),
                query_string=set_operation_querystring,
            )

        if self._orderbys:
            querystring += self._orderby_sql(sql_context)

        querystring += self._limit_offset_sql(sql_context)

        if sql_context.subquery:
            querystring = f"({querystring})"

        if sql_context.with_alias:
            return sql_context.format_alias_sql(
                querystring,
                self.alias,
            )

        return querystring

    def _orderby_sql(self, sql_context: SqlContext) -> str:
        """Renders the ORDER BY clause - a term selected under an alias is ordered by the alias."""
        clauses = []
        selected_aliases = {select_term.alias for select_term in self.base_query._selects}
        term_context = sql_context.copy(subquery=True)
        for field, directionality in self._orderbys:
            term = (
                sql_context.quote(field.alias)
                if field.alias and field.alias in selected_aliases
                else field.get_sql(term_context)
            )

            clauses.append(
                f"{term} {sql_context.dialect.renderers.get_order_sql(directionality)}"
                if directionality is not None
                else term
            )

        return " ORDER BY {orderby}".format(orderby=",".join(clauses))
