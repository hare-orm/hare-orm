from __future__ import annotations

from contextlib import nullcontext
from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.exceptions import (
    QueryError,
)
from hare.query.expressions.arithmetic.combinable_expression import CombinableExpression
from hare.query.expressions.constants import UNBINDABLE_VALUE_ORIGIN
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType
from hare.sql import SqlContext
from hare.sql.terms.term import Term

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.field import Field as ModelField
    from hare.models import Model
    from hare.query.statements import AwaitableQuery
    from hare.sql.builder.queries.query_builder import QueryBuilder
    from hare.sql.builder.tables.selectable import Selectable
from hare.query.expressions.subqueries.outer_scope import OuterScope


class Subquery(CombinableExpression, Term):  # type: ignore[misc]
    """A queryset embedded as a SQL subquery - a Term usable where one is accepted
    (``.filter(id__in=Subquery(...))``, ``.annotate(x=Subquery(...))``) and an Expression: resolved
    as a filter or annotation value, it builds its query while the outer query's context is active,
    so an ``OuterReference`` inside finds it.
    """

    # The result is decoded by the field of the one column the subquery selects; it stays raw when
    # there is no such field.
    populate_field_object = True
    is_subquery = True
    holds_nested_query = True

    plan_parts: ClassVar[DeclaredPlanParts] = (
        ("query", PlanPartType.QUERY),
        ("source_query", PlanPartType.NONE),
        ("shares_outer_scope", PlanPartType.NONE),
        # The name a query selects it under - its annotation's key.
        ("alias", PlanPartType.NONE),
        ("_query", PlanPartType.NONE),
        ("_built_query", PlanPartType.NONE),
        ("is_aggregate", PlanPartType.NONE),
        ("outer_reference_terms", PlanPartType.NONE),
    )

    def __init__(self, query: AwaitableQuery[Any], *, shares_outer_scope: bool = False) -> None:
        """
        Args:
            query: The queryset or query embedded.
            shares_outer_scope: Whether an ``OuterReference(...)`` of the query refers to the query the
                enclosing one is nested in, not to the enclosing one - for a subquery hare adds
                itself between a queryset and its own filters (``distinct(*fields)`` numbering its
                rows), which SQL lets reach any enclosing query.
        """
        super().__init__()
        #: The query given - taken by the query building its SQL when first used, so a queryset
        #: made before ``Hare.init()`` is taken once init applied the calls it kept.
        self.source_query = query
        self.shares_outer_scope = shares_outer_scope
        self._query: AwaitableQuery[Any] | None = None
        # The wrapped query as last built into SQL - a per-build copy bound to a connection, so
        # building never pins the caller's own queryset to whichever connection was current.
        self._built_query: AwaitableQuery[Any] | None = None

    @property
    def query(self) -> AwaitableQuery[Any]:
        """The query building the subquery's SQL - a queryset selecting values is built by its
        values query."""
        if self._query is None:
            query = self._query = cast("AwaitableQuery[Any]", self.source_query._get_compiler())
            if query is not self.source_query:
                # Its values come from the query given.
                query._plan_origin = self.source_query
        return self._query

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        built_query = self._get_wrapped_query(self.query, expression_context)
        value_wrapper_references = expression_context.value_wrapper_references
        # A subquery sharing the outer scope is built in the scope already open: its OuterRefs - and
        # what they report - go to the query the enclosing one is nested in.
        with nullcontext() if self.shares_outer_scope else OuterScope.entered(expression_context) as scope:
            if value_wrapper_references is not None and built_query.plannable:
                # The enclosing query keeps its plan with this subquery in it: the subquery's
                # values are recorded among the enclosing query's own.
                built_query._make_subquery(value_wrapper_references=value_wrapper_references)
            else:
                built_query._make_subquery()
                if value_wrapper_references is not None:
                    value_wrapper_references.append((UNBINDABLE_VALUE_ORIGIN, None))
        self._built_query = built_query
        if scope is None:
            self.is_aggregate = False
            self.outer_reference_terms = ()
            return ExpressionResult(term=self, output_field=self.get_output_field())
        self.is_aggregate = bool(scope.aggregate_references)
        self.outer_reference_terms = tuple(scope.reference_terms)
        return ExpressionResult(term=self, joins=scope.extra_joins, output_field=self.get_output_field())

    def get_selected_names(self) -> list[str] | None:
        """The output names of the columns a values()/values_list() subquery selects.

        Returns:
            The names, or None when the subquery isn't a values query.
        """
        # Local import: the query statements import the expressions.
        from hare.query.statements.select.combined_query import CombinedQuery
        from hare.query.statements.select.values.values_output import ValuesOutput
        from hare.query.statements.select.values_query import ValuesQuery

        query = self.query
        if isinstance(query, CombinedQuery):
            return list(query._get_output_names()) if query._combines_values else None
        if isinstance(query, ValuesQuery):
            return list(ValuesOutput.get_output_names_for_set_operation(query))
        return None

    def raise_if_selects_more_columns(self, filter_key: str, expected_column_count: int) -> None:
        """Raises when the subquery selects more columns than the filter compares.

        Args:
            filter_key: The filter it is the value of.
            expected_column_count: The number of columns the filter compares.

        Raises:
            QueryError: The subquery selects more columns.
        """
        selected_names = self.get_selected_names()
        if selected_names is None or len(selected_names) <= expected_column_count:
            return
        raise QueryError(
            f"Cannot use multi-field values as a filter value: '{filter_key}' compares "
            f"{expected_column_count} column(s), but the subquery selects {len(selected_names)} "
            f"({', '.join(selected_names)}). Select only the compared field, e.g. "
            f".values_list('{selected_names[0]}', flat=True)."
        )

    def get_output_field(self) -> ModelField[Any] | None:
        """The field of the one column this subquery selects, when it is a
        ``values()``/``values_list()`` query selecting exactly one.

        Returns:
            The field, None when it can't be told.
        """
        # Local import: the query statements import the expressions.
        from hare.query.statements.select.combined.combined_derived_queries import CombinedDerivedQueries

        # Local import: the query statements import the expressions.
        from hare.query.statements.select.combined_query import CombinedQuery
        from hare.query.statements.select.values.values_output import ValuesOutput
        from hare.query.statements.select.values_query import ValuesQuery

        query = self._built_query or self.query
        if isinstance(query, CombinedQuery):
            return CombinedDerivedQueries.get_single_column_value_field(query)
        if not isinstance(query, ValuesQuery):
            return None
        fields_for_select = list(ValuesOutput.get_output_field_names(query))
        if len(fields_for_select) != 1:
            return None
        return self.get_selected_field(fields_for_select[0])

    def get_selected_field(self, selected_name: str) -> ModelField[Any] | None:
        """The field of a column a ``values()``/``values_list()`` subquery selects.

        Args:
            selected_name: The column's output name.

        Returns:
            The field, None when it can't be told.
        """
        from hare.query.functions.aggregates.count import Count
        from hare.query.statements.select.values_query import ValuesQuery

        query = self._built_query or self.query
        if not isinstance(query, ValuesQuery):
            return None
        if (annotation_output_field := query._annotation_output_fields.get(selected_name)) is not None:
            return annotation_output_field
        if selected_name in query._annotations:
            # Count() never populates an output field of its own (its result is the same integer
            # on every backend as a bare value, but an aggregate wrapped around it isn't).
            if isinstance(query._annotations[selected_name], Count):
                return Count.COUNT_OUTPUT_FIELD  # type:ignore[arg-type]
            return None
        return self.get_path_field(query.model, selected_name)

    @staticmethod
    def get_path_field(model: type[Model], path: str) -> ModelField[Any] | None:
        """The field a selected ``field`` or ``relation__field`` path reads.

        Args:
            model: The model the path starts from.
            path: The selected name.

        Returns:
            The field at the end of the path, or None when the path names no field.
        """
        field = model._meta.fields_map.get(path)
        if field is not None:
            return field
        relation_name, __, rest_path = path.partition("__")
        if not rest_path or relation_name not in model._meta.fetch_fields:
            return None
        related_model = model._meta.fields_map[relation_name].related_model  # type:ignore[attr-defined]
        return Subquery.get_path_field(related_model, rest_path)

    def _build_if_needed(self) -> None:
        # Used as a raw Term, never resolved as an expression: an OuterReference inside has no outer
        # context and raises.
        if self._built_query is None:
            built_query = self.query._get_execution_query()
            built_query._make_subquery()
            self._built_query = built_query

    def get_nested_query(self) -> QueryBuilder | None:
        """The query builder the subquery was built into, None before it is built."""
        built_query = self._built_query
        return None if built_query is None else built_query.query

    def get_sql(self, sql_context: SqlContext) -> str:
        self._build_if_needed()
        return cast("AwaitableQuery[Any]", self._built_query).query.get_sql(sql_context)

    def as_(self, alias: str) -> Selectable:  # type: ignore[override]
        self._build_if_needed()
        aliased_query = cast("AwaitableQuery[Any]", self._built_query).query.as_(alias)
        # The aliased query is what lands in SELECT - it keeps this subquery's grouping facts.
        aliased_query.is_subquery = True
        aliased_query.is_aggregate = self.is_aggregate
        aliased_query.outer_reference_terms = self.outer_reference_terms
        return aliased_query
