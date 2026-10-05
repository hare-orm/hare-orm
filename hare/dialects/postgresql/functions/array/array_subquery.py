from __future__ import annotations

from typing import TYPE_CHECKING, Any, Self

from hare.exceptions import QueryError, UnSupportedError
from hare.fields.data.containers.array_field import ArrayField
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.expressions.subqueries.subquery import Subquery
from hare.sql import SqlContext

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.field import Field


class ArraySubquery(Subquery):
    """``ARRAY(SELECT ...)`` - the one column a ``values()``/``values_list()`` queryset selects, of
    every row, as an array, in the queryset's ``order_by()``::

        Book.objects.annotate(
            tag_names=ArraySubquery(Tag.objects.filter(books=OuterReference("pk")).order_by("name").values("name"))
        )

    The value reads as a list of the column's values - an empty list for no row. PostgreSQL only:
    another dialect raises ``UnSupportedError`` before the query is sent.

    Raises:
        QueryError: The queryset selects anything but one column.
    """

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        selected_names = self.get_selected_names()
        if selected_names is None or len(selected_names) != 1:
            raise QueryError(
                "ArraySubquery takes a queryset selecting one column - .values('<field>') or "
                f".values_list('<field>', flat=True), got {selected_names or 'model rows'}"
            )
        return super().get_result(expression_context)

    def get_output_field(self) -> Field[Any] | None:
        element_field = super().get_output_field()
        if element_field is None:
            return None
        return ArrayField(base_field=element_field)

    def as_(self, alias: str) -> Self:  # type: ignore[override]
        self.alias = alias
        return self

    def get_subquery_sql(self, sql_context: SqlContext) -> str:
        """The SQL of the wrapped query, without an alias."""
        return Subquery.get_sql(self, sql_context.copy(with_alias=False))

    def get_sql(self, sql_context: SqlContext) -> str:
        renderer = sql_context.dialect.renderers.get(type(self))
        if renderer is None:
            raise UnSupportedError(f"ArraySubquery has no SQL for the {sql_context.dialect} dialect - it needs arrays")
        return (
            sql_context.format_alias_sql(renderer(self, sql_context), self.alias)
            if sql_context.with_alias
            else renderer(self, sql_context)
        )
