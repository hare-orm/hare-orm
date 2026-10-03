from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.fields.data.boolean import BooleanField
from hare.query.expressions.base.expression import Expression
from hare.query.expressions.base.expression_context import ExpressionContext
from hare.query.expressions.base.expression_result import ExpressionResult
from hare.query.expressions.value_refs.value_ref_types import RecordedValueRefs
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.sql.terms.base.term import Term
from hare.sql.terms.criteria.not_criterion import Not

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.expressions.q import Q
    from hare.query.queryset import QuerySet
from hare.query.expressions.exists_term import ExistsTerm
from hare.query.expressions.outer_scope import OuterScope


class Exists(Expression):
    """EXISTS(SELECT 1 FROM ... WHERE ...) - correlated if the child QuerySet uses OuterRef(...)
    in its .filter(...). Use via ``.annotate(x=Exists(...)).filter(x=True)`` (Exists is a boolean
    value, not something you compare via ``field=Exists(...)``).

    Example:
        ::

            Report.objects.annotate(
                has_open_ticket=Exists(Ticket.objects.filter(report_id=OuterRef("id"), status="open"))
            ).filter(has_open_ticket=True)
    """

    # The result is decoded as a bool - SQLite returns 0/1.
    populate_field_object = True
    # One shared instance, not a fresh BooleanField() per get_result() call - every statement
    # plan of an Exists() annotation refers to this same output field instead of each holding a
    # throwaway field of its own.
    OUTPUT_FIELD = BooleanField()

    def __init__(self, queryset: QuerySet[Any, Any], *, negated: bool = False) -> None:

        #: The queryset given - taken by the query building its SQL when first used, so a
        #: queryset made before ``Hare.init()`` is taken once init applied the calls it kept.
        self.source_queryset = queryset
        self._queryset: Any = None
        self.negated = negated

    @property
    def queryset(self) -> Any:
        """The queryset the subquery is built from - a queryset selecting values is built by
        its values query."""
        from hare.query.statements.select.values_query import ValuesQuery

        if self._queryset is None:
            compiler = self.source_queryset._get_compiler()
            self._queryset = compiler if isinstance(compiler, ValuesQuery) else self.source_queryset
        return self._queryset

    def __invert__(self) -> Exists:
        """Returns a ``NOT EXISTS(...)`` copy - never changes self, which annotate() calls may share."""
        return Exists(self.source_queryset, negated=not self.negated)

    def __and__(self, other: Any) -> Q:
        """Returns a ``Q`` matching both this condition and ``other`` (a ``Q`` or ``Exists``)."""
        from hare.query.expressions.q import Q

        return Q(self) & other

    def __or__(self, other: Any) -> Q:
        """Returns a ``Q`` matching this condition or ``other`` (a ``Q`` or ``Exists``)."""
        from hare.query.expressions.q import Q

        return Q(self) | other

    def get_plan_description(self, context: PlanContext) -> PlanDescription | None:
        """The negation and the wrapped queryset's model, annotations, filters, grouping and
        ``.none()`` - the build records the inner query's values among the enclosing query's own.

        Args:
            context: The context the condition is resolved in - the inner query resolves names
                against its own annotations.

        Returns:
            The description, None for a wrapped ``.values()`` query, which records no values, or
            a wrapped queryset with CTEs or a slice, or one keeping no plan of its own.
        """
        inner = self.queryset
        if self.wraps_values_query or inner._with_ctes or inner._limit is not None or inner._offset:
            return None
        compiler = inner._get_compiler()
        if not compiler._query_state_is_plannable():
            return None
        filters_description = compiler._get_filters_plan_description()
        if filters_description is None:
            return None
        # The connection the wrapped queryset is pinned to: one pinned to another connection than
        # the enclosing query's is refused when built, so it never shares a plan built without it.
        return PlanDescription(
            (
                Exists,
                self.negated,
                inner.model,
                inner.get_pinned_connection_name(),
                filters_description.structure,
                inner._group_bys,
                inner._is_none,
            ),
            filters_description.values,
        )

    @property
    def wraps_values_query(self) -> bool:
        """Whether the wrapped query is a ``.values()``/``.values_list()`` query, embedded with its
        own select list, grouping and HAVING instead of as a ``SELECT 1`` exists query."""
        from hare.query.statements.select.values_query import ValuesQuery

        return isinstance(self.queryset, ValuesQuery)

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        if self.wraps_values_query:
            return self._get_values_query_result(expression_context)
        inner = self._get_wrapped_query(self.queryset.exists(), expression_context)
        with OuterScope.entered(expression_context) as scope:
            if expression_context.value_wrapper_refs is not None:
                # The outer query is recording its plan: the inner query's value references go into
                # the outer's, in the order Exists.get_plan_description() lists the inner values.
                inner_refs: RecordedValueRefs = []
                inner._make_query(value_wrapper_refs=inner_refs)
                expression_context.value_wrapper_refs.extend(inner_refs)
            else:
                inner._make_query()
            inner._apply_none_as_subquery()
        return self._get_exists_result(inner.query, scope)

    def _get_values_query_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        """Resolves EXISTS over a ``.values()``/``.values_list()`` query.

        Args:
            expression_context: The outer query's resolve context.

        Returns:
            The EXISTS criterion, the joins the outer query needs and its boolean output field.
        """
        inner = self._get_wrapped_query(self.queryset, expression_context)
        with OuterScope.entered(expression_context) as scope:
            inner._make_subquery()
        return self._get_exists_result(inner.query, scope)

    def _get_exists_result(self, inner_query: Any, scope: OuterScope) -> ExpressionResult:
        """Wraps a built inner query into this condition's EXISTS criterion.

        Args:
            inner_query: The built inner query.
            scope: What building it collected about the outer query.

        Returns:
            The EXISTS (or NOT EXISTS) criterion with its joins and boolean output field.
        """
        term: Term = ExistsTerm(inner_query)
        term.is_aggregate = bool(scope.aggregate_references)
        term.outer_reference_terms = tuple(scope.reference_terms)
        if self.negated:
            term = Not(term)
        # A Field kept as a class attribute of a non-Model class - outside what Field.__get__'s
        # overloads describe.
        return ExpressionResult(
            term=term,
            joins=scope.extra_joins,
            output_field=self.OUTPUT_FIELD,  # type:ignore[call-overload]
        )
