from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from hare.sql.context import DEFAULT_SQL_CONTEXT, SqlContext
from hare.sql.enums import Order, SetOperation
from hare.sql.exceptions import SetOperationException
from hare.sql.queries.tables.cte import Cte
from hare.sql.queries.tables.selectable import Selectable
from hare.sql.terms.base.term import Term
from hare.sql.terms.base.value_wrapper import ValueWrapper
from hare.sql.terms.field import Field
from hare.sql.utils import builder

if TYPE_CHECKING:
    from typing import Self
from hare.sql.queries.builder.pagination_sql_mixin import PaginationSqlMixin
from hare.sql.queries.builder.query_builder import QueryBuilder


class SetOperationQuery(PaginationSqlMixin, Selectable, Term):  # type:ignore[misc]
    """
    A query of a set operation - UNION (DISTINCT or ALL), INTERSECT or EXCEPT.

    Created by `QueryBuilder.union()`, `union_all()`, `intersect()`, `except_of()` and `minus()`.

    This class should not be instantiated directly.
    """

    def __init__(
        self,
        base_query: QueryBuilder,
        set_operation_query: QueryBuilder,
        set_operation: SetOperation,
        alias: str | None = None,
        wrapper_cls: type[ValueWrapper] = ValueWrapper,
    ) -> None:
        super().__init__(alias)
        self.base_query = base_query
        self._set_operation = [(set_operation, set_operation_query)]
        self._orderbys: list[tuple[Term, Order | None]] = []

        self._limit: ValueWrapper | None = None
        self._offset: ValueWrapper | None = None

        self._wrapper_cls = wrapper_cls
        # CTEs of the whole set operation - a branch's own WITH would land after UNION.
        self._with: list[Cte] = []

    def __copy__(self) -> Self:
        # The lists are copied, or every operation would change them for every branch built from one
        # base.
        newone = type(self).__new__(type(self))
        newone.__dict__.update(self.__dict__)
        newone._set_operation = list(self._set_operation)
        newone._orderbys = list(self._orderbys)
        newone._with = list(self._with)
        return newone

    @builder
    def orderby(self, *fields: Field | str, **kwargs: Any) -> Self:  # type:ignore[return]
        for field in fields:
            order_term = (
                Field(field, table=self.base_query._from[0])
                if isinstance(field, str)
                else self.base_query.wrap_constant(field)
            )

            self._orderbys.append((order_term, kwargs.get("order")))

    @builder
    def limit(self, limit: int) -> Self:  # type:ignore[return]
        self._limit = cast("ValueWrapper", self.wrap_constant(limit))

    @builder
    def offset(self, offset: int) -> Self:  # type:ignore[return]
        self._offset = cast("ValueWrapper", self.wrap_constant(offset))

    @builder
    def union(self, other: Selectable) -> Self:  # type:ignore[return]
        self._set_operation.append((SetOperation.UNION, other))  # type:ignore[arg-type]

    @builder
    def union_all(self, other: Selectable) -> Self:  # type:ignore[return]
        self._set_operation.append((SetOperation.UNION_ALL, other))  # type:ignore[arg-type]

    @builder
    def intersect(self, other: Selectable) -> Self:  # type:ignore[return]
        self._set_operation.append((SetOperation.INTERSECT, other))  # type:ignore[arg-type]

    @builder
    def except_of(self, other: Selectable) -> Self:  # type:ignore[return]
        self._set_operation.append((SetOperation.EXCEPT_OF, other))  # type:ignore[arg-type]

    def __add__(self, other: Selectable) -> Self:  # type:ignore[override]
        return self.union(other)

    def __mul__(self, other: Selectable) -> Self:  # type:ignore[override]
        return self.union_all(other)

    def __str__(self) -> str:
        return self.get_sql(DEFAULT_SQL_CONTEXT)

    def get_sql(self, ctx: SqlContext) -> str:
        set_operation_template = " {type} {query_string}"

        # The base query's dialect and quote character.
        ctx = ctx.copy(
            dialect=self.base_query.QUERY_CLS.SQL_CONTEXT.dialect,
            quote_char=self.base_query.QUERY_CLS.SQL_CONTEXT.quote_char,
            parameterizer=ctx.parameterizer,
        )
        set_ctx = ctx.copy(subquery=self.base_query.wrap_set_operation_queries)

        # Rendered once, at the front, before the branches - its parameters come first, as its text
        # does.
        with_prefix = QueryBuilder._with_sql(self._with, ctx) if self._with else ""
        base_querystring = self.base_query.get_sql(set_ctx)
        querystring = with_prefix + base_querystring
        for set_operation, set_operation_query in self._set_operation:
            set_operation_querystring = set_operation_query.get_sql(set_ctx)

            if len(self.base_query._selects) != len(set_operation_query._selects):
                raise SetOperationException(
                    "Queries must have an equal number of select statements in a set operation."
                    f"\n\nMain Query:\n{base_querystring}\n\nSet Operations Query:\n{set_operation_querystring}"
                )

            querystring += set_operation_template.format(type=set_operation, query_string=set_operation_querystring)

        if self._orderbys:
            querystring += self._orderby_sql(ctx)

        querystring += self._limit_sql(ctx)
        querystring += self._offset_sql(ctx)

        if ctx.subquery:
            querystring = f"({querystring})"

        if ctx.with_alias:
            return ctx.format_alias_sql(
                querystring,
                self.alias or self._table_name,  # type:ignore[arg-type]
            )

        return querystring

    def _orderby_sql(self, ctx: SqlContext) -> str:
        """Renders the ORDER BY clause - a term selected under an alias is ordered by the alias."""
        clauses = []
        selected_aliases = {s.alias for s in self.base_query._selects}
        term_ctx = ctx.copy(subquery=True)
        for field, directionality in self._orderbys:
            term = (
                ctx.quote(field.alias) if field.alias and field.alias in selected_aliases else field.get_sql(term_ctx)
            )

            clauses.append(f"{term} {directionality}" if directionality is not None else term)

        return " ORDER BY {orderby}".format(orderby=",".join(clauses))
