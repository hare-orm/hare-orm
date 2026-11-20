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
    from hare.core.hare_context import HareContext
    from hare.dialects.base.dialect import Dialect
    from hare.fields.field import Field as ModelField
    from hare.models import Model
    from hare.query.expressions.value_references.value_reference_types import ValueReference
    from hare.query.plans.recording.scope_value_source import ScopeValueSource
    from hare.sql.sql_context import SqlContext


class StatementPlan:
    """How a query of one plan key runs: the SQL text built once, where each parameter comes from, and
    what reading the result needs - a later query of the key only binds its values.

    A parameter is a value bound through the references the build recorded with its origin
    (``PlanOrigins``) - as many as the build resolved it - the query's LIMIT or OFFSET, or a constant
    of the plan. A query whose text could differ from the plan's - a value rendered as a
    literal, a value of another type, a LIMIT or OFFSET on one side only, a value no reference binds
    - is built in full. Preparation returns no reusable plan when the text holds a parameter
    that came from no term, or a CTE whose values the plan cannot bind.

    A fixed positional model projection retains its shared column layout, prepared before the
    plan is published. Connections and loaded instances remain local to each execution.
    """

    __slots__ = (
        "parameter_bindings",
        "sql",
        "parameters",
        "has_limit",
        "has_offset",
        "limit_binding",
        "offset_binding",
        "result_reading",
        "annotation_output_fields",
        "scope_sources",
    )

    #: The classes of values no reference binds - a term or an expression renders into the SQL
    #: text itself, a query is a subquery. Completed with the query classes once they are
    #: defined (they import this module).
    unbindable_value_classes: ClassVar[tuple[type, ...]] = (Term,)

    if TYPE_CHECKING:
        parameter_bindings: Any
        sql: str
        parameters: list[Any]
        has_limit: bool
        has_offset: bool
        limit_binding: ParameterSlot | None
        offset_binding: ParameterSlot | None
        result_reading: Any
        annotation_output_fields: tuple[tuple[str, ModelField[Any] | None], ...]
        scope_sources: tuple[ScopeValueSource, ...]

    @classmethod
    def prepare(
        cls,
        template: QueryBuilder | SetOperationQuery,
        value_sources: tuple[tuple[ValueReference, ...], ...],
        repeated_values: tuple[tuple[int, int], ...] = (),
        annotation_output_fields: tuple[tuple[str, ModelField[Any] | None], ...] = (),
        result_reading: Any = None,
        scope_sources: tuple[ScopeValueSource, ...] = (),
        *,
        sql_context: SqlContext | None = None,
        slice_boundary_terms: tuple[Term | None, Term | None] | None = None,
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
            annotation_output_fields: The output fields of the annotations whose results are
                decoded through a field.
            result_reading: What else a query of the key needs to read its result, kept for its
                type (the column converters of a set operation, say).
            scope_sources: The default scopes folded into its JOINs - their references follow
                the description's in ``value_sources``.
            sql_context: The selected connection's rendering settings, captured when the plan is built.
            slice_boundary_terms: The limit and offset wrappers created by this query, before
                extensions or derived-table wrapping replace its final builder boundaries.
                None uses the template boundaries. A replaced wrapper may have no rendered
                positions; it never acquires an extension's unrelated parameter positions.

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
        if prepared_sources is None or any(source is None for source in parameterizer.sources):
            return rendered, None
        plan = cls()
        plan.sql = sql
        plan.parameter_bindings = NativeModules.rows.ParameterBindings(
            tuple(tuple(reference.bind for reference in sources) for sources in prepared_sources), repeated_values
        )
        plan.parameters = parameterizer.values.copy()
        for position in preparation.bound_positions:
            plan.parameters[position] = None
        limit, offset = (template._limit, template._offset) if slice_boundary_terms is None else slice_boundary_terms
        plan.has_limit = limit is not None
        plan.has_offset = offset is not None
        plan.limit_binding = preparation.slot(limit) if limit is not None else None
        plan.offset_binding = preparation.slot(offset) if offset is not None else None
        plan.result_reading = result_reading
        plan.annotation_output_fields = annotation_output_fields
        plan.scope_sources = scope_sources
        return rendered, plan

    def get_scope_values(self, *, application_context: HareContext | None = None) -> list[Any] | None:
        """The values of the default scopes folded into the plan's JOINs, as the current context
        makes them.

        Returns:
            A fresh list of values, None when a scope has another structure now.
        """
        values: list[Any] = []
        for scope_source in self.scope_sources:
            scope_values = scope_source.get_values(application_context=application_context)
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
        *,
        application_context: HareContext | None = None,
    ) -> list[Any] | None:
        """The parameters of one query of the plan key, in the order of the plan's SQL text.

        Args:
            values: The query's values in description order. Current values from scopes in JOINs
                are appended in the order expected by the prepared parameter bindings.
            model: The model queried.
            dialect: The dialect the query runs on.
            limit: The query's LIMIT (None for none); ``PLAN_CACHE_MISS`` keeps the plan's own.
            offset: The query's OFFSET, the same way.
            application_context: The context used to bind current default-scope values in joined relations.

        Returns:
            The parameters, or None when the query's SQL text would differ from the plan's,
            including a change in the structure of a JOIN scope.
        """
        if self.scope_sources:
            scope_values = self.get_scope_values(application_context=application_context)
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
        return slot is not None and slot.bind(parameters, value)
