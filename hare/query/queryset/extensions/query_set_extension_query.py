from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.exceptions import QueryError
from hare.query.expressions import Expression, ExpressionContext
from hare.query.expressions.f import F
from hare.sql import JoinType

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.dialect import Dialect
    from hare.models import Model
    from hare.query.expressions.conditions.q import Q
    from hare.query.expressions.conditions.query_modifier import QueryModifier
    from hare.query.expressions.expression_result import ExpressionResult, TableCriterionTuple
    from hare.query.expressions.value_references.value_reference_types import RecordedValueReferences
    from hare.query.statements.awaitable_query import AwaitableQuery
    from hare.sql import Table
    from hare.sql.builder.queries.query_builder import QueryBuilder


class QuerySetExtensionQuery:
    """The query a dialect's QuerySet method is applied to (``QuerySetExtension.reads_query``) - its
    model, connection and base table, and its conditions and fields resolved as the query resolves
    its own: their values are bound and recorded into the query's plan like its filters' values.
    """

    __slots__ = ("query", "value_wrapper_references")

    def __init__(self, query: AwaitableQuery[Any], value_wrapper_references: RecordedValueReferences | None) -> None:
        """
        Args:
            query: The query, its ``query`` builder built.
            value_wrapper_references: The list the query's value references are recorded into, None when
                none are.
        """
        self.query = query
        self.value_wrapper_references = value_wrapper_references

    @property
    def model(self) -> type[Model]:
        """The query's model."""
        return self.query.model

    @property
    def connection(self) -> DatabaseClient:
        """The connection the query runs on."""
        return self.query._connection

    @property
    def dialect(self) -> Dialect:
        """The dialect of the query's connection."""
        return self.query.dialect

    @property
    def table(self) -> Table:
        """The table the query's own fields are read from."""
        return self.query._effective_basetable()

    @property
    def is_write(self) -> bool:
        """Whether the query is an ``UPDATE`` or a ``DELETE``."""
        query_builder = self.query.query
        return query_builder._update_table is not None or bool(query_builder._delete_from)

    @property
    def is_summary(self) -> bool:
        """Whether the query computes a value of its rows - ``count()``, ``exists()``, ``aggregate()``."""
        # Local import: the summary queries import the queryset package this module is part of.
        from hare.query.statements.summary.rows_summary_query import RowsSummaryQuery

        return isinstance(self.query, RowsSummaryQuery)

    def get_expression_context(self) -> ExpressionContext:
        """The context the query's conditions and fields resolve in.

        Returns:
            The context.
        """
        query = self.query
        return ExpressionContext(
            model=query.model,
            dialect=query.dialect,
            connection=query._connection,
            table=query._effective_basetable(),
            annotations=query._annotations,
            value_wrapper_references=self.value_wrapper_references,
            select_related_extra_conditions=query._select_related_extra_conditions,
            multi_valued_join_generations={},
            visibility=query._visibility,
        )

    def get_condition(self, condition: Q) -> QueryModifier:
        """Resolves a condition as the query's filters are resolved.

        Args:
            condition: The condition.

        Returns:
            Its criteria and the JOINs they need.
        """
        return condition.get_result(self.get_expression_context())

    def get_expression(self, expression: str | Expression) -> ExpressionResult:
        """Resolves a field, an annotation or an expression of the query.

        Args:
            expression: A field path or annotation name, or an expression.

        Returns:
            Its term and the JOINs it needs.

        Raises:
            QueryError: ``expression`` is neither.
        """
        if isinstance(expression, str):
            expression = F(expression)
        if not isinstance(expression, Expression):
            raise QueryError(f"Expected a field name or an expression, got {expression!r}")
        return expression.get_result(self.get_expression_context())

    def join(self, builder: QueryBuilder, joins: list[TableCriterionTuple]) -> QueryBuilder:
        """Adds the JOINs a resolved condition or expression needs - each table once.

        Args:
            builder: The query's builder.
            joins: The JOINs.

        Returns:
            The builder with them.
        """
        query = self.query
        for table, criterion in joins:
            if table not in query._joined_tables_set:
                builder = builder.join(table, how=JoinType.LEFT_OUTER).on(criterion)
                query._joined_tables.append(table)
                query._joined_tables_set.add(table)
        return builder

    def get_built_builder(self) -> QueryBuilder:
        """The query's builder, built - built now when the query ran on a plan's SQL text.

        Returns:
            The builder.
        """
        query = self.query
        if query._compiled_statement is not None:
            query._runs_compiled_statement = False
            query._make_query()
        return query.query
