from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from hare.fields.data.boolean_field import BooleanField
from hare.query.expressions.constants import UNBINDABLE_VALUE_ORIGIN
from hare.query.expressions.expression import Expression
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.expressions.value_references.value_reference_types import RecordedValueReferences
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType
from hare.sql.terms.criteria.not_criterion import Not
from hare.sql.terms.term import Term

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.expressions.conditions.q import Q
    from hare.query.queryset import QuerySet
from hare.query.expressions.subqueries.outer_scope import OuterScope
from hare.sql.terms.subqueries.exists_term import ExistsTerm


class Exists(Expression):
    """EXISTS(SELECT 1 FROM ... WHERE ...) - correlated if the child QuerySet uses OuterReference(...)
    in its .filter(...). Use via ``.annotate(x=Exists(...)).filter(x=True)`` (Exists is a boolean
    value, not something you compare via ``field=Exists(...)``).

    Example:
        ::

            Report.objects.annotate(
                has_open_ticket=Exists(Ticket.objects.filter(report_id=OuterReference("id"), status="open"))
            ).filter(has_open_ticket=True)
    """

    # The result is decoded as a bool - SQLite returns 0/1.
    populate_field_object = True
    # One shared instance, not a fresh BooleanField() per get_result() call - every statement
    # plan of an Exists() annotation refers to this same output field instead of each holding a
    # throwaway field of its own.
    OUTPUT_FIELD = BooleanField()

    #: The negation and the query built into the condition - its values among the enclosing
    #: query's own.
    plan_parts: ClassVar[DeclaredPlanParts] = (
        ("negated", PlanPartType.KEY),
        ("source_queryset", PlanPartType.NONE),
        ("_queryset", PlanPartType.NONE),
        ("get_condition_query", PlanPartType.QUERY_METHOD),
    )

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
        if self._queryset is None:
            from hare.query.statements.select.values_query import ValuesQuery

            compiler = self.source_queryset._get_compiler()
            self._queryset = compiler if isinstance(compiler, ValuesQuery) else self.source_queryset
        return self._queryset

    def __invert__(self) -> Exists:
        """Returns a ``NOT EXISTS(...)`` copy - never changes self, which annotate() calls may share."""
        return Exists(self.source_queryset, negated=not self.negated)

    def __and__(self, other: Any) -> Q:
        """Returns a ``Q`` matching both this condition and ``other`` (a ``Q`` or ``Exists``)."""
        from hare.query.expressions.conditions.q import Q

        return Q(self) & other

    def __or__(self, other: Any) -> Q:
        """Returns a ``Q`` matching this condition or ``other`` (a ``Q`` or ``Exists``)."""
        from hare.query.expressions.conditions.q import Q

        return Q(self) | other

    @property
    def wraps_values_query(self) -> bool:
        """Whether the wrapped query is a ``.values()``/``.values_list()`` query, embedded with its
        own select list, grouping and HAVING instead of as a ``SELECT 1`` exists query."""
        from hare.query.statements.select.values_query import ValuesQuery

        return isinstance(self.queryset, ValuesQuery)

    def get_condition_query(self) -> Any:
        """The query built into the condition (``get_built_query()``).

        Returns:
            The query.
        """
        return self.get_built_query(self.queryset)

    def get_built_query(self, inner: Any) -> Any:
        """The query built into the condition: a ``.values()`` query itself, else the queryset's
        ``exists()`` query.

        Args:
            inner: The wrapped queryset or values query.

        Returns:
            The query.
        """
        if self.wraps_values_query:
            return inner
        if inner._pending_filter_calls:
            # Built once, so every copy holds the same conditions (_build_conditions_for_copies()).
            # Imported here: the filter calls import the expressions package.
            from hare.query.queryset.pending_calls.pending_filter_calls import PendingFilterCalls

            PendingFilterCalls.build_pending_filter_calls(inner)
        exists_query = inner.exists()
        # Made again for each description and build - its values come from the wrapped queryset.
        exists_query._plan_origin = inner
        return exists_query

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        if self.wraps_values_query:
            return self._get_values_query_result(expression_context)
        inner = self._get_wrapped_query(self.get_built_query(self.queryset), expression_context)
        with OuterScope.entered(expression_context) as scope:
            if expression_context.value_wrapper_references is not None:
                # The outer query is recording its plan: the inner query's value references go into
                # the outer's.
                inner_references: RecordedValueReferences = []
                inner._make_query(value_wrapper_references=inner_references)
                expression_context.value_wrapper_references.extend(inner_references)
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
        value_wrapper_references = expression_context.value_wrapper_references
        with OuterScope.entered(expression_context) as scope:
            if value_wrapper_references is not None and inner.plannable:
                inner._make_subquery(value_wrapper_references=value_wrapper_references)
            else:
                inner._make_subquery()
                if value_wrapper_references is not None:
                    value_wrapper_references.append((UNBINDABLE_VALUE_ORIGIN, None))
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
