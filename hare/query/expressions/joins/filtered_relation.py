from __future__ import annotations

from copy import copy
from typing import TYPE_CHECKING, Any, ClassVar

from hare.exceptions import FieldError, QueryError
from hare.fields.relations.fields.relational_field import RelationalField
from hare.query.expressions.aggregate_paths.aggregated_multi_valued_paths import AggregatedMultiValuedPaths
from hare.query.expressions.conditions.q import Q
from hare.query.expressions.conditions.query_modifier import QueryModifier
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.expressions.f import F
from hare.query.expressions.joins.named_join import NamedJoin
from hare.query.lookup_info.lookup_paths import LookupPaths
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.expressions.expression_result import TableCriterionTuple
    from hare.sql import Table


class FilteredRelation(NamedJoin):
    """A relation joined under a name of its own with a condition in its ``ON`` clause -
    ``qs.alias(vegetarian=FilteredRelation("pizzas", condition=Q(pizzas__vegetarian=True)))``. The name
    then reads the relation's rows that match the condition, in filters, expressions, aggregates,
    ``values()`` and ``order_by()`` (``vegetarian__name``, ``Count("vegetarian")``); a row without one
    still comes, its values NULL - a LEFT JOIN. It is never selected itself, and the relation's own
    name keeps its own JOIN.

    Args:
        relation_name: The relation - a field path (``pizzas``, ``order__lines``).
        condition: The rows of the relation kept - its keys start with ``relation_name``
            (``Q(pizzas__vegetarian=True)``); every row without it.

    Raises:
        QueryError: ``relation_name`` isn't a non-empty string, or ``condition`` isn't a ``Q``.
    """

    plannable = True

    #: The relation and its condition as the related model reads it - the condition's values are
    #: bound into the JOINs it is folded into. The name it is joined under is the annotation's key.
    plan_parts: ClassVar[DeclaredPlanParts] = (
        ("relation_name", PlanPartType.KEY),
        ("name", PlanPartType.NONE),
        ("condition", PlanPartType.NONE),
        ("_join_condition", PlanPartType.NONE),
        ("get_join_condition", PlanPartType.JOIN_CONDITION_METHOD),
    )

    def __init__(self, relation_name: str, *, condition: Q | None = None) -> None:
        if not isinstance(relation_name, str) or not relation_name:
            raise QueryError(f"FilteredRelation takes a relation name, got {relation_name!r}")
        if condition is not None and not isinstance(condition, Q):
            raise QueryError(f"FilteredRelation(condition=...) takes a Q, got {condition!r}")
        self.relation_name = relation_name
        self.condition = condition if condition is not None else Q()
        # The condition as the related model reads it, made on first use - one object, whose values
        # its description and the JOINs it is folded into record under the same origins.
        self._join_condition: Q | None = None

    def get_join_condition(self) -> Q | None:
        """The condition as the related model reads it - its keys without the relation's path.

        Returns:
            The condition, None when it keeps every row.

        Raises:
            QueryError: A key or an ``F()`` reads something other than the relation's fields.
        """
        if not self.condition:
            return None
        if self._join_condition is None:
            self._join_condition = self.get_relative_condition(self.condition)
        return self._join_condition

    def get_relative_condition(self, condition: Q) -> Q:
        """``condition`` with the relation's path taken off its keys and ``F()`` values.

        Args:
            condition: A condition over the relation.

        Returns:
            The condition over the related model.

        Raises:
            QueryError: See ``get_join_condition()``.
        """
        if condition.expression is not None:
            raise QueryError("FilteredRelation(condition=...) can't hold an Exists(...)")
        relative = copy(condition)
        relative.children = tuple(self.get_relative_condition(child) for child in condition.children)
        relative.filters = {
            self.get_relative_path(key): self.get_relative_value(value) for key, value in condition.filters.items()
        }
        return relative

    def get_relative_path(self, path: str) -> str:
        """A path over the relation, without the relation's own path.

        Raises:
            QueryError: The path doesn't go through the relation.
        """
        prefix = f"{self.relation_name}__"
        if not path.startswith(prefix) or path == prefix:
            raise QueryError(
                f"FilteredRelation({self.relation_name!r}) condition reads {path!r} - every key and F() of it must "
                f"start with '{prefix}'"
            )
        return path.removeprefix(prefix)

    def get_relative_value(self, value: Any) -> Any:
        """A condition value with its ``F()`` references taken off the relation's path."""
        if isinstance(value, F):
            return F(self.get_relative_path(value.name))
        if isinstance(value, (list, tuple)):
            return type(value)(self.get_relative_value(item) for item in value)
        return value

    def get_relation_joins(
        self, expression_context: ExpressionContext
    ) -> tuple[Any, Table, list[TableCriterionTuple]]:
        """The JOINs of the relation, its last one under this relation's own name with the condition.

        Args:
            expression_context: The context of the queried model.

        Returns:
            The related model, its joined table and the JOINs.

        Raises:
            FieldError: ``relation_name`` isn't a relation of the model.
        """
        if self.name is None:
            raise QueryError("A FilteredRelation is used through the name alias()/annotate() gives it")
        model = expression_context.model
        table = expression_context.table
        joins: list[TableCriterionTuple] = []
        segments = self.relation_name.split("__")
        for index, segment in enumerate(segments):
            relation = model._meta.fields_map.get(segment)
            if not isinstance(relation, RelationalField) or segment not in model._meta.fetch_fields:
                raise FieldError(
                    f"FilteredRelation({self.relation_name!r}): {segment!r} isn't a relation of {model.__name__}"
                )
            is_last = index == len(segments) - 1
            hop_joins = LookupPaths.get_scoped_joins(
                table,
                relation,
                self.name if is_last else segment,
                visibility=expression_context.visibility,
                extra_condition=self.get_join_condition() if is_last else None,
                dialect=expression_context.dialect,
                connection=expression_context.connection,
            )
            joins.extend(hop_joins)
            table = hop_joins[-1][0]
            model = relation.related_model
        return model, table, joins

    def get_path_result(self, path: str, expression_context: ExpressionContext) -> ExpressionResult:
        """What ``<name>__<path>`` reads - a field of the related model, a further relation's, or with
        no path the related primary key.

        Args:
            path: The path after the name.
            expression_context: The context of the queried model.

        Returns:
            The term, its field and the JOINs.
        """
        related_model, related_table, joins = self.get_relation_joins(expression_context)
        self.record_multi_valued_paths(expression_context, related_model, path)
        term, path_joins, output_field = LookupPaths.get_nested_field(
            related_model,
            related_table,
            path or related_model._meta.primary_key_attribute_names[0],
            visibility=expression_context.visibility,
            dialect=expression_context.dialect,
            connection=expression_context.connection,
        )
        return ExpressionResult(term=term, joins=joins + path_joins, output_field=output_field)

    def is_multi_valued(self, model: Any) -> bool:
        """Whether a hop of the relation from ``model`` is to-many - its JOIN may repeat a row."""
        for segment in self.relation_name.split("__"):
            relation = model._meta.fields_map.get(segment)
            if not isinstance(relation, RelationalField):
                return False
            if relation.is_multi_valued:
                return True
            model = relation.related_model
        return False

    def record_multi_valued_paths(self, expression_context: ExpressionContext, related_model: Any, path: str) -> None:
        """Records the to-many JOINs a path through the relation makes - its own under its name, a
        JOIN of its own - for the check of aggregates another JOIN fans out.

        Args:
            expression_context: The context of the queried model.
            related_model: The related model.
            path: The path after the name.
        """
        tracker = AggregatedMultiValuedPaths.get_from(expression_context)
        if tracker is None or self.name is None:
            return
        if self.is_multi_valued(expression_context.model):
            tracker.record_path(self.name)
        tracker.record_lookup(related_model, path, self.name)

    def get_path_filter(
        self,
        expression_context: ExpressionContext,
        path: str,
        value: Any,
        filter_call_generation: int,
        value_origin: tuple[Any, ...] | None = None,
    ) -> QueryModifier:
        """The relation's JOIN with its condition, then the filter on the related model's rows."""
        related_model, related_table, joins = self.get_relation_joins(expression_context)
        filter_key = self.get_filter_key(path, related_model)
        self.record_multi_valued_paths(expression_context, related_model, filter_key)
        return self.get_joined_row_filter(
            expression_context,
            related_model,
            related_table,
            joins,
            filter_key,
            value,
            filter_call_generation,
            value_origin,
        )

    def __repr__(self) -> str:
        return f"FilteredRelation({self.relation_name!r}, condition={self.condition!r})"
