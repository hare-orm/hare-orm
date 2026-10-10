from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from hare.exceptions import FieldError, QueryError
from hare.fields.relations.fields.relational_field import RelationalField
from hare.query.expressions.constants import (
    MAX_RECURSIVE_DEPTH,
    RECURSIVE_ROWS_CTE_NAME,
    RECURSIVE_ROWS_DEPTH_COLUMN,
    RECURSIVE_ROWS_PREVIOUS_ALIAS,
    RECURSIVE_ROWS_START_ALIAS,
)
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.expressions.subqueries.declarations import KeyRowsQuery
from hare.query.expressions.subqueries.subquery import Subquery
from hare.query.expressions.value_references.expression_arguments import ExpressionArguments
from hare.query.key_columns import KeyColumns
from hare.query.lookup_info.lookup_paths import LookupPaths
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType
from hare.sql import Table
from hare.sql.terms.field import Field
from hare.sql.terms.tuple import Tuple
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.expressions.expression_context import ExpressionContext
    from hare.query.queryset.queryset import QuerySet


class RecursiveRows(KeyRowsQuery):
    """The primary keys of the rows reachable from a queryset's rows through a relation of the model to
    itself, followed again and again - the queryset's rows included: a ``WITH RECURSIVE`` subquery, for
    ``pk__in=``. ``QuerySet.with_recursive()`` builds it. A row reached twice comes once, so a cycle
    ends; the rows the related model's default scope hides aren't walked through.

    Args:
        start: The rows the walk starts from.
        relation_name: The relation - forward, reverse or many-to-many - of the model to itself.
        max_depth: How many steps from the start rows - 0 for them alone; None for no limit.

    Raises:
        QueryError: ``relation_name`` isn't a non-empty string, or ``max_depth`` isn't None or an
            int in ``0..MAX_RECURSIVE_DEPTH``.
    """

    #: The relation, the start rows' keys - their values bound - and the depth, bound when limited;
    #: the walk's JOINs record the default scopes they fold in like any other.
    plan_parts: ClassVar[DeclaredPlanParts] = (
        ("relation_name", PlanPartType.KEY),
        ("start", PlanPartType.NONE),
        ("get_start_keys", PlanPartType.EXPRESSION_METHOD),
        ("max_depth", PlanPartType.ARGUMENT),
    )

    def __init__(self, start: QuerySet[Any, Any], relation_name: str, *, max_depth: int | None = None) -> None:
        if not isinstance(relation_name, str) or not relation_name:
            raise QueryError(f"with_recursive() takes a relation name, got {relation_name!r}")
        if max_depth is not None and (
            isinstance(max_depth, bool) or not isinstance(max_depth, int) or not 0 <= max_depth <= MAX_RECURSIVE_DEPTH
        ):
            raise QueryError(
                f"with_recursive(max_depth=...) takes None or an int in 0..{MAX_RECURSIVE_DEPTH}, got {max_depth!r}"
            )
        self.start = start
        self.relation_name = relation_name
        self.max_depth = max_depth

    def get_start_keys(self) -> Subquery:
        """The primary keys of the rows the walk starts from.

        Returns:
            The subquery.
        """
        self.start._build_conditions_for_copies()
        start_keys_query = self.start._get_field_values_query("pk")
        # Made again for each description and build - its values come from the start rows.
        start_keys_query._plan_origin = self.start
        return Subquery(start_keys_query)

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        model = self.start.model
        meta = model._meta
        relation = meta.fields_map.get(self.relation_name)
        if (
            not isinstance(relation, RelationalField)
            or self.relation_name not in meta.fetch_fields
            or relation.related_model is not model
        ):
            raise FieldError(
                f"with_recursive({self.relation_name!r}): it isn't a relation of {model.__name__} to itself"
            )
        key_columns = KeyColumns.get_source_columns(meta)
        has_depth = self.max_depth is not None
        walk = Table(RECURSIVE_ROWS_CTE_NAME)

        # The query class of the connection: a set operation renders in its base query's dialect.
        connection = expression_context.connection if expression_context.connection is not None else meta.connection
        query_class = connection.query_class

        start_table = meta.basetable.as_(RECURSIVE_ROWS_START_ALIAS)
        start_keys = self.get_start_keys().get_result(expression_context).term
        start_key = (
            start_table[key_columns[0]]
            if len(key_columns) == 1
            else Tuple(*(start_table[key_column] for key_column in key_columns))
        )
        base = (
            query_class.from_(start_table, wrap_set_operation_queries=False)
            .select(
                *(start_table[column] for column in key_columns),
                *([ValueWrapper(0, allow_parametrize=False)] if has_depth else []),
            )
            .where(start_key.isin(start_keys))
        )

        previous_table = meta.basetable.as_(RECURSIVE_ROWS_PREVIOUS_ALIAS)
        relation_joins = LookupPaths.get_scoped_joins(
            previous_table,
            relation,
            self.relation_name,
            visibility=expression_context.visibility,
            dialect=expression_context.dialect,
            connection=expression_context.connection,
        )
        next_table = relation_joins[-1][0]
        step = (
            query_class.from_(walk)
            .join(previous_table)
            .on(
                KeyColumns.row_equality(
                    [walk[key_column] for key_column in key_columns],
                    [previous_table[key_column] for key_column in key_columns],
                )
            )
        )
        for joined_table, join_criterion in relation_joins:
            step = step.join(joined_table).on(join_criterion)
        step = step.select(
            *(next_table[column] for column in key_columns),
            *([walk[RECURSIVE_ROWS_DEPTH_COLUMN] + ValueWrapper(1, allow_parametrize=False)] if has_depth else []),
        )
        if has_depth:
            max_depth = ExpressionArguments.get_result(self, "max_depth", self.max_depth, expression_context).term
            step = step.where(walk[RECURSIVE_ROWS_DEPTH_COLUMN] < max_depth)

        column_names = [*key_columns, *([RECURSIVE_ROWS_DEPTH_COLUMN] if has_depth else [])]
        rows = (
            query_class.with_(base + step, RECURSIVE_ROWS_CTE_NAME, *(Field(name) for name in column_names))
            .from_(walk)
            .select(*(walk[column] for column in key_columns))
        )
        return ExpressionResult(term=rows, joins=[])
