from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from hare.classes.value_descriptions import ValueDescriptions
from hare.exceptions import QueryError, UnSupportedError
from hare.query.enums import Lookup
from hare.query.expressions.conditions.q import Q
from hare.query.expressions.conditions.query_modifier import QueryModifier
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.expressions.joins.named_join import NamedJoin
from hare.query.expressions.subqueries.outer_reference import OuterReference
from hare.query.expressions.subqueries.outer_scope import OuterScope
from hare.query.expressions.subqueries.subquery import Subquery
from hare.query.lookup_info.lookup_paths import LookupPaths
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType
from hare.query.plans.recording.plan_recording import PlanRecording
from hare.sql.builder.tables.asof_join_source import AsofJoinSource

if TYPE_CHECKING:
    from hare.core.apps.declarations import ModelReference
    from hare.models import Model
    from hare.query.expressions.expression_result import TableCriterionTuple
    from hare.query.expressions.value_references.value_reference_types import RecordedValueReferences
    from hare.query.queryset.queryset import QuerySet
    from hare.sql.builder.tables.selectable import Selectable
    from hare.sql.terms.criteria.criterion import Criterion


class AsofJoin(NamedJoin):
    """A model's rows joined ``ASOF`` under the name ``alias()``/``annotate()`` gives it - each row
    with the one row of the model closest to it by an inequality, among the rows equal to it by the
    other conditions::

        Trade.objects.alias(
            quote=AsofJoin(Quote, on=Q(symbol=OuterReference("symbol"), quoted_at__lte=OuterReference("traded_at")))
        ).values("symbol", "traded_at", "quote__price")

    ``<name>__<field>`` reads a field of the joined row in filters, expressions, ``values()`` and
    ``order_by()``; a row without one still comes, its values NULL - an ``ASOF LEFT JOIN``. It is
    never selected itself. ``on`` compares fields of the joined model with ``OuterReference()`` fields of the
    queried row: at least one equality (``symbol=OuterReference("symbol")``) and one inequality (``__lt``,
    ``__lte``, ``__gt``, ``__gte``) of the field the closest row is found by. A queryset in place of
    the model joins its rows. A database without ``Features.supports_asof_join`` raises
    ``UnSupportedError`` before the query is sent.

    Args:
        source: The joined model - its class, its ``"app.Model"`` label or a ``swappable()`` setting - or a
            queryset of its rows.
        on: The equalities and the one inequality.

    Raises:
        QueryError: ``on`` isn't a ``Q`` of AND-ed equalities and one inequality of the joined
            model's fields with ``OuterReference()``; ``source`` is neither a model nor a queryset of model
            rows.
    """

    plannable = True

    #: The joined model or queryset and the condition - its values are bound into the JOIN.
    plan_parts: ClassVar[DeclaredPlanParts] = (
        ("model", PlanPartType.KEY),
        ("name", PlanPartType.NONE),
        ("source", PlanPartType.NONE),
        ("on", PlanPartType.NONE),
        ("_join_condition", PlanPartType.NONE),
        ("get_source_subquery", PlanPartType.EXPRESSION_METHOD),
        ("get_join_condition", PlanPartType.JOIN_CONDITION_METHOD),
    )

    #: The inequalities the closest row is found by.
    INEQUALITY_LOOKUPS: ClassVar[frozenset[str]] = frozenset({Lookup.LT, Lookup.LTE, Lookup.GT, Lookup.GTE})

    def __init__(self, source: ModelReference | QuerySet[Any, Any], *, on: Q) -> None:
        # Local imports: the models, the queryset package and the context import this module.
        from hare.core.hare_context import HareContext
        from hare.fields.relations.swappable_model_reference import SwappableModelReference
        from hare.models import Model
        from hare.query.queryset.query_specification import QuerySpecification

        if isinstance(source, (str, SwappableModelReference)):
            source = HareContext.require_current().get_model(source)
        if isinstance(source, type) and issubclass(source, Model):
            self.model: type[Model] = source
        elif isinstance(source, QuerySpecification) and source._selection is None:
            self.model = source.model
        else:
            raise QueryError(f"AsofJoin() joins a model or a queryset of its rows, got {source!r}")
        if not isinstance(on, Q):
            raise QueryError(f"AsofJoin(on=...) takes a Q, got {ValueDescriptions.get_shown_value(on)}")
        self.source = source
        self.on = on
        # The condition with its equalities first and its inequality last, made on first use - one
        # object, whose values its description and the JOIN record under the same origins.
        self._join_condition: Q | None = None

    def get_source_subquery(self) -> Subquery | None:
        """The subquery of the joined queryset's rows.

        Returns:
            The subquery, None when a model is joined.
        """
        if isinstance(self.source, type):
            return None
        return Subquery(self.source)

    def get_join_condition(self) -> Q:
        """The condition of the JOIN - its equalities, then its inequality.

        Returns:
            The condition.

        Raises:
            QueryError: ``on`` isn't AND-ed equalities and one inequality of the joined model's
                fields with ``OuterReference()``.
        """
        if self._join_condition is not None:
            return self._join_condition
        on = self.on
        if on.expression is not None or on.children or on._is_negated or not on.filters:
            raise QueryError(
                "AsofJoin(on=...) takes filters of the joined model's fields - "
                "Q(<field>=..., <field>__lte=...) - with no OR, NOT, nested Q or expression"
            )
        equalities = {}
        inequalities = {}
        fields_map = self.model._meta.fields_map
        for key, value in on.filters.items():
            if not isinstance(value, OuterReference):
                raise QueryError(
                    f"AsofJoin(on=...) compares {key!r} with a field of the queried row - OuterReference(...), "
                    f"got {ValueDescriptions.get_shown_value(value)}"
                )
            field_name, _separator, lookup = key.rpartition("__")
            if lookup in self.INEQUALITY_LOOKUPS and field_name in fields_map:
                inequalities[key] = value
            elif key in fields_map:
                equalities[key] = value
            else:
                raise QueryError(
                    f"AsofJoin(on=...) reads {key!r} - a field of {self.model.__name__} compared with = or with "
                    "one of __lt, __lte, __gt, __gte"
                )
        if len(inequalities) != 1 or not equalities:
            raise QueryError(
                "AsofJoin(on=...) finds the closest row by one inequality (__lt, __lte, __gt, __gte) among the "
                f"rows of at least one equality - got {len(inequalities)} and {len(equalities)}"
            )
        self._join_condition = Q(**equalities, **inequalities)
        return self._join_condition

    def get_path_result(self, path: str, expression_context: ExpressionContext) -> ExpressionResult:
        """What ``<name>__<path>`` reads - a field of the joined row, with no path its primary key.

        Raises:
            QueryError: It is used before ``alias()``/``annotate()`` named it, or its name alone reads
                a composite primary key.
            UnSupportedError: The database joins nothing ``ASOF``.
        """
        return self.get_field_result(path or self.get_primary_key_name(self.model), expression_context)

    def get_joins_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        # The first column of the primary key, a JOIN of any key.
        return self.get_field_result(self.model._meta.primary_key_attribute_names[0], expression_context)

    def get_field_result(self, field_path: str, expression_context: ExpressionContext) -> ExpressionResult:
        """What a field of the joined row reads.

        Args:
            field_path: The field's path in the joined model.
            expression_context: The context of the queried model.

        Returns:
            The term, its field and the JOINs.

        Raises:
            QueryError: It is used before ``alias()``/``annotate()`` named it.
            UnSupportedError: The database joins nothing ``ASOF``.
        """
        if self.name is None:
            raise QueryError("An AsofJoin is used through the name alias()/annotate() gives it")
        # A context of no connection only probes which names a query reads - the query run checks.
        connection = expression_context.connection
        if connection is not None and not connection.features.supports_asof_join:
            raise UnSupportedError(
                f"AsofJoin {self.name!r} needs a join of the closest row, which {expression_context.dialect} doesn't "
                "have (Features.supports_asof_join)"
            )
        joins = self.get_joins(expression_context)
        term, path_joins, output_field = LookupPaths.get_nested_field(
            self.model,
            joins[-1][0],
            field_path,
            visibility=expression_context.visibility,
            dialect=expression_context.dialect,
            connection=expression_context.connection,
        )
        return ExpressionResult(term=term, joins=joins + path_joins, output_field=output_field)

    def get_joins(self, expression_context: ExpressionContext) -> list[TableCriterionTuple]:
        """The JOINs: those its condition's ``OuterReference()`` through a relation needs, then its own.

        Args:
            expression_context: The context of the queried model.

        Returns:
            The JOINs.
        """
        name = self.name
        subquery = self.get_source_subquery()
        source: Selectable = (
            self.model._meta.basetable if subquery is None else subquery.get_result(expression_context).term  # type: ignore[assignment]
        )
        joined = AsofJoinSource(name, source)  # type: ignore[arg-type]
        # The condition's values are bound from the query holding the JOIN when a plan runs.
        value_wrapper_references: RecordedValueReferences | None = [] if PlanRecording.is_recording() else None
        outer_context_token = OuterScope.current_expression_context.set(expression_context)
        outer_joins: list[TableCriterionTuple] = []
        outer_joins_token = OuterScope.current_extra_joins.set(outer_joins)
        try:
            modifier = self.get_join_condition().get_result(
                ExpressionContext(
                    model=self.model,
                    table=joined,  # type: ignore[arg-type]
                    annotations={},
                    value_wrapper_references=value_wrapper_references,
                    dialect=expression_context.dialect,
                    connection=expression_context.connection,
                )
            )
        finally:
            OuterScope.current_extra_joins.reset(outer_joins_token)
            OuterScope.current_expression_context.reset(outer_context_token)
        if value_wrapper_references is not None:
            PlanRecording.record_join_condition(value_wrapper_references)
        if modifier.joins:
            raise QueryError(f"AsofJoin(on=...) reads fields of {self.model.__name__} itself, not of a relation")
        criterion: Criterion = modifier.where_criterion
        return [*outer_joins, (joined, criterion)]  # type: ignore[list-item]

    def get_path_filter(
        self,
        expression_context: ExpressionContext,
        path: str,
        value: Any,
        filter_call_generation: int,
        value_origin: tuple[Any, ...] | None = None,
    ) -> QueryModifier:
        """The JOIN, then the filter on the joined row."""
        joins = self.get_joins(expression_context)
        filter_key = self.get_filter_key(path, self.model)
        return self.get_joined_row_filter(
            expression_context,
            self.model,
            joins[-1][0],
            joins,
            filter_key,
            value,
            filter_call_generation,
            value_origin,
        )

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        return self.get_path_result("", expression_context)

    def is_multi_valued(self, model: Any) -> bool:
        """An ``ASOF`` JOIN joins at most one row to each row - it repeats none."""
        return False
