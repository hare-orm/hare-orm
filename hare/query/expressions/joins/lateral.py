from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from hare.exceptions import FieldError, QueryError, UnSupportedError
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.expressions.joins.named_columns import NamedColumns
from hare.query.expressions.subqueries.subquery import Subquery
from hare.sql.builder.tables.lateral_query import LateralQuery
from hare.sql.terms.criteria.true_criterion import TrueCriterion

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.expressions.expression_context import ExpressionContext
    from hare.query.queryset.queryset import QuerySet
    from hare.query.statements.awaitable_query import AwaitableQuery
    from hare.sql import Table
    from hare.sql.builder.tables.selectable import Selectable


class Lateral(NamedColumns):
    """A ``values()``/``values_list()`` queryset joined ``LATERAL`` under the name ``alias()``/``annotate()``
    gives it - a subquery run for each row, reading the row through ``OuterReference()``, several columns at once::

        Author.objects.alias(
            latest=Lateral(
                Book.objects.filter(author=OuterReference("pk")).order_by("-id").values("name", "rating")[:1]
            )
        ).values("name", "latest__name", "latest__rating")

    ``<name>__<column>`` reads a column the queryset selects, in filters, expressions, ``values()`` and
    ``order_by()``; a row the subquery returns nothing for still comes, its columns NULL - a LEFT JOIN.
    A row comes once for each row the subquery returns. It is never selected itself. PostgreSQL only:
    another database raises ``UnSupportedError`` before the query is sent.

    Args:
        query: The queryset - ``values()``/``values_list()``.

    Raises:
        QueryError: ``query`` isn't a ``values()``/``values_list()`` queryset.
    """

    def __init__(self, query: QuerySet[Any, Any]) -> None:
        # Local import: the queryset package imports this module.
        from hare.query.queryset.query_specification import QuerySpecification

        if not isinstance(query, QuerySpecification) or query._selection is None:
            raise QueryError(
                f"Lateral() takes a values()/values_list() queryset - the columns its name reads, got {query!r}"
            )
        self.query = query

    def get_path_result(self, path: str, expression_context: ExpressionContext) -> ExpressionResult:
        """The column ``path`` names, read from the subquery joined under the name.

        Raises:
            QueryError: It is used before ``alias()``/``annotate()`` named it.
            UnSupportedError: The database has no ``LATERAL``.
            FieldError: ``path`` isn't a column the queryset selects.
        """
        if self.name is None:
            raise QueryError("A Lateral is used through the name alias()/annotate() gives it")
        # A context of no connection only probes which names a query reads - the query run checks.
        connection = expression_context.connection
        if connection is not None and not connection.features.supports_lateral:
            raise UnSupportedError(
                f"Lateral {self.name!r} needs a LATERAL subquery, which {expression_context.dialect} doesn't have"
            )
        subquery = Subquery(cast("AwaitableQuery[Any]", self.query))
        selected_names = subquery.get_selected_names() or []
        if path not in selected_names:
            raise FieldError(
                f"Lateral {self.name!r} reads one of the columns its queryset selects ({', '.join(selected_names)}) "
                f"as {self.name}__<column>, got {path!r}"
            )
        lateral_query = LateralQuery(self.name, cast("Selectable", subquery.get_result(expression_context).term))
        return ExpressionResult(
            term=lateral_query.field(path),
            joins=[(cast("Table", lateral_query), TrueCriterion())],
            output_field=subquery.get_selected_field(path),
        )

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        # The name read alone (an alias a path uses) is its JOIN - values() refuses to select it.
        selected_names = Subquery(cast("AwaitableQuery[Any]", self.query)).get_selected_names() or []
        return self.get_path_result(selected_names[0], expression_context)

    def __repr__(self) -> str:
        return f"Lateral({self.query!r})"
