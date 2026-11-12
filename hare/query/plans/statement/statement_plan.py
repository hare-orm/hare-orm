from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, ClassVar, Self

from hare.native.native_modules import NativeModules
from hare.query.constants import PLAN_CACHE_MISS
from hare.query.plans.statement.declarations import RenderedStatement
from hare.query.plans.statement.parameter_preparation import ParameterPreparation
from hare.query.plans.statement.parameter_slot import ParameterSlot
from hare.sql.builder.queries.query_builder import QueryBuilder
from hare.sql.builder.queries.set_operation_query import SetOperationQuery
from hare.sql.terms.parameters.recording_parameterizer import RecordingParameterizer
from hare.sql.terms.term import Term

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
    from hare.fields.field import Field as ModelField
    from hare.models import Model
    from hare.query.expressions.value_references.value_reference_types import ValueReference
    from hare.query.plans.recording.scope_value_source import ScopeValueSource
    from hare.query.rows.native.hydration_layout import HydrationEntry
    from hare.sql.sql_context import SqlContext


class StatementPlan:
    """How a query of one plan key runs: the SQL text built once, where each parameter comes from, and
    what reading the result needs - a later query of the key only binds its values.

    A parameter is a value bound through the references the build recorded with its origin
    (``PlanOrigins``) - as many as the build resolved it - the query's LIMIT or OFFSET, or a constant
    of the plan. A query whose text could differ from the plan's - a value rendered as a
    literal, a value of another type, a LIMIT or OFFSET on one side only, a value no reference binds
    - is built in full. The text isn't reused (``sql`` is None) when it holds a parameter that came
    from no term, or a CTE whose values the plan doesn't bind.

    A fixed positional model projection retains its shared column layout, prepared before the
    plan is published. Connections and loaded instances remain local to each execution.
    """

    __slots__ = (
        "value_sources",
        "parameter_bindings",
        "sql",
        "parameters",
        "has_limit",
        "has_offset",
        "limit_binding",
        "offset_binding",
        "result_reading",
        "result_column_names",
        "decode_plan",
        "decode_plan_key",
        "decode_plan_is_partial",
        "select_related_positions",
        "annotation_output_fields",
        "scope_sources",
    )

    #: The classes of values no reference binds - a term or an expression renders into the SQL
    #: text itself, a query is a subquery. Completed with the query classes once they are
    #: defined (they import this module).
    unbindable_value_classes: ClassVar[tuple[type, ...]] = (Term,)

    if TYPE_CHECKING:
        value_sources: tuple[tuple[ValueReference, ...], ...]
        parameter_bindings: Any
        sql: str | None
        parameters: list[Any]
        has_limit: bool
        has_offset: bool
        limit_binding: ParameterSlot | None
        offset_binding: ParameterSlot | None
        result_reading: Any
        result_column_names: tuple[str, ...]
        decode_plan: tuple[HydrationEntry, ...] | None
        decode_plan_key: tuple[str | None, ...] | None
        decode_plan_is_partial: bool
        select_related_positions: tuple[Any, ...]
        annotation_output_fields: tuple[tuple[str, ModelField[Any] | None], ...]
        scope_sources: tuple[ScopeValueSource, ...]

    @classmethod
    def prepare(
        cls,
        template: QueryBuilder | SetOperationQuery,
        value_sources: tuple[tuple[ValueReference, ...], ...],
        repeated_values: tuple[tuple[int, int], ...] = (),
        decode_plan: tuple[HydrationEntry, ...] | None = None,
        decode_plan_is_partial: bool = False,
        select_related_positions: tuple[Any, ...] = (),
        annotation_output_fields: tuple[tuple[str, ModelField[Any] | None], ...] = (),
        decode_plan_key: tuple[str | None, ...] | None = None,
        binds_ctes: bool = False,
        result_reading: Any = None,
        scope_sources: tuple[ScopeValueSource, ...] = (),
        *,
        sql_context: SqlContext | None = None,
        result_column_names: tuple[str, ...] = (),
    ) -> tuple[RenderedStatement, Self | None]:
        """Render once for this execution and prepare a detached plan from the same recording.

        Args:
            template: The query the first query of the key built.
            value_sources: Per value, in the order the query's description lists them
                (``Plannable.get_plan_description()``), the references the build recorded with its
                origin - none for a value listed again under an origin listed before.
            repeated_values: The values listed again under an origin listed before, each with the
                position of the first - one object read twice, so a later query binds the plan only
                when its values there are one object too.
            decode_plan: The row decode plan of a model query.
            decode_plan_is_partial: Whether the decode plan covers only some fields.
            select_related_positions: The ``select_related()`` row layout of a model query.
            annotation_output_fields: The output fields of the annotations whose results are
                decoded through a field.
            decode_plan_key: The names of the base model's selected columns a model query reads
                its rows by - a part of the plan's key.
            binds_ctes: Whether the values of the template's CTEs are among ``value_sources`` - the
                plan key then holds the structure of each CTE body.
            result_reading: What else a query of the key needs to read its result, kept for its
                type (the column converters of a set operation, say).
            scope_sources: The default scopes folded into its JOINs - their references follow
                the description's in ``value_sources``.
            sql_context: The selected connection's rendering settings, captured when the plan is built.

        Returns:
            The current statement and its reusable plan, or None for the plan when a recorded
            source cannot be rebound. Current parameter values belong only to the statement.
        """
        parameterizer = RecordingParameterizer()
        # A set operation renders in the context of its first branch's query class.
        context_query = template.base_query if isinstance(template, SetOperationQuery) else template
        sql_context = (sql_context or context_query.query_class.SQL_CONTEXT).copy(parameterizer=parameterizer)
        sql = template.get_sql(sql_context)
        preparation = ParameterPreparation(parameterizer, sql_context.dialect.parameters, sql_context.dialect.types)
        prepared_sources = preparation.prepare_inputs(value_sources)
        rendered = RenderedStatement(sql, parameterizer.values)
        if (
            prepared_sources is None
            or any(source is None for source in parameterizer.sources)
            or (not binds_ctes and template._with)
        ):
            return rendered, None
        plan = cls()
        plan.sql = sql
        plan.value_sources = prepared_sources
        plan.parameter_bindings = NativeModules.rows.ParameterBindings(
            tuple(tuple(reference.bind for reference in sources) for sources in prepared_sources), repeated_values
        )
        plan.parameters = parameterizer.values.copy()
        for position in preparation.bound_positions:
            plan.parameters[position] = None
        plan.has_limit = template._limit is not None
        plan.has_offset = template._offset is not None
        plan.limit_binding = preparation.slot(template._limit) if template._limit is not None else None
        plan.offset_binding = preparation.slot(template._offset) if template._offset is not None else None
        plan.result_reading = result_reading
        plan.result_column_names = result_column_names
        plan.decode_plan = decode_plan
        plan.decode_plan_key = decode_plan_key
        plan.decode_plan_is_partial = decode_plan_is_partial
        plan.select_related_positions = select_related_positions
        plan.annotation_output_fields = annotation_output_fields
        plan.scope_sources = scope_sources
        return rendered, plan

    def get_scope_values(self) -> list[Any] | None:
        """The values of the default scopes folded into the plan's JOINs, as the current context
        makes them.

        Returns:
            A fresh list of values, None when a scope has another structure now.
        """
        values: list[Any] = []
        for scope_source in self.scope_sources:
            scope_values = scope_source.get_values()
            if scope_values is None:
                return None
            values += scope_values
        return values

    def bind(
        self,
        values: Sequence[Any],
        model: type[Model],
        dialect: Dialect,
        limit: Any = PLAN_CACHE_MISS,
        offset: Any = PLAN_CACHE_MISS,
    ) -> list[Any] | None:
        """The parameters of one query of the plan key, in the order of the plan's SQL text.

        Args:
            values: The query's values in description order, before the current JOIN-scope values
                appended here to match ``value_sources``.
            model: The model queried.
            dialect: The dialect the query runs on.
            limit: The query's LIMIT (None for none); ``PLAN_CACHE_MISS`` keeps the plan's own.
            offset: The query's OFFSET, the same way.

        Returns:
            The parameters, or None when the query's SQL text would differ from the plan's,
            including a change in the structure of a JOIN scope.
        """
        if self.sql is None:
            return None
        if self.scope_sources:
            scope_values = self.get_scope_values()
            if scope_values is None:
                return None
            scope_values[:0] = values
            values = scope_values
        parameters = self.parameter_bindings.bind(
            values, self.parameters, self.unbindable_value_classes, model, dialect
        )
        if parameters is None:
            return None
        if limit is not PLAN_CACHE_MISS and not StatementPlan.bind_boundary(
            parameters, limit, self.has_limit, self.limit_binding
        ):
            return None
        if offset is not PLAN_CACHE_MISS and not StatementPlan.bind_boundary(
            parameters, offset, self.has_offset, self.offset_binding
        ):
            return None
        return parameters

    @staticmethod
    def bind_boundary(parameters: list[Any], value: Any, present: bool, slot: ParameterSlot | None) -> bool:
        """Binds a query's LIMIT or OFFSET.

        Args:
            parameters: The parameters being bound.
            value: The query's LIMIT or OFFSET, None for none.
            present: Whether the plan's SQL text has it.
            slot: Its parameter slot, None when the text holds no parameter for it.

        Returns:
            False when the query's SQL text would differ from the plan's.
        """
        if value is None:
            return not present
        return slot is not None and bool(slot.positions) and slot.bind(parameters, value)
