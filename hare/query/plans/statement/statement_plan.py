from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, ClassVar

from hare.query.constants import PLAN_CACHE_MISS
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
    """

    __slots__ = (
        "value_sources",
        "repeated_values",
        "sql",
        "parameters",
        "has_limit",
        "has_offset",
        "limit_binding",
        "offset_binding",
        "result_reading",
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

    def __init__(
        self,
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
    ) -> None:
        """Renders ``template`` once, recording each parameter's source.

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
        """
        self.repeated_values = repeated_values
        parameterizer = RecordingParameterizer()
        # A set operation renders in the context of its first branch's query class.
        context_query = template.base_query if isinstance(template, SetOperationQuery) else template
        sql_context = (sql_context or context_query.query_class.SQL_CONTEXT).copy(parameterizer=parameterizer)
        sql = template.get_sql(sql_context)
        preparation = ParameterPreparation(parameterizer, sql_context.dialect.parameters)
        prepared_sources = preparation.prepare_inputs(value_sources)
        self.value_sources = prepared_sources or ()
        self.sql: str | None = (
            sql
            if prepared_sources is not None
            and all(source is not None for source in parameterizer.sources)
            and (binds_ctes or not template._with)
            else None
        )
        self.parameters = parameterizer.values.copy()
        for position in preparation.bound_positions:
            self.parameters[position] = None
        self.has_limit = template._limit is not None
        self.has_offset = template._offset is not None
        self.limit_binding = preparation.slot(template._limit) if template._limit is not None else None
        self.offset_binding = preparation.slot(template._offset) if template._offset is not None else None
        self.result_reading = result_reading
        self.decode_plan = decode_plan
        self.decode_plan_key = decode_plan_key
        self.decode_plan_is_partial = decode_plan_is_partial
        self.select_related_positions = select_related_positions
        self.annotation_output_fields = annotation_output_fields
        self.scope_sources = scope_sources

    def get_scope_values(self) -> list[Any] | None:
        """The values of the default scopes folded into the plan's JOINs, as the current context
        makes them.

        Returns:
            The values, None when a scope has another structure now.
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
            values: The query's values, in the order of ``value_sources``.
            model: The model queried.
            dialect: The dialect the query runs on.
            limit: The query's LIMIT (None for none); ``PLAN_CACHE_MISS`` keeps the plan's own.
            offset: The query's OFFSET, the same way.

        Returns:
            The parameters, or None when the query's SQL text would differ from the plan's.
        """
        if self.sql is None or len(values) != len(self.value_sources):
            return None
        for position, first_position in self.repeated_values:
            if values[position] is not values[first_position]:
                return None
        parameters = self.parameters.copy()
        unbindable_value_classes = self.unbindable_value_classes
        for references, value in zip(self.value_sources, values, strict=True):
            if value is None or isinstance(value, unbindable_value_classes):
                return None
            for reference in references:
                parameter_values = reference.get_parameter_values(value, model, dialect)
                if parameter_values is None:
                    return None
                for slot, parameter_value, always_parameterized in parameter_values:
                    # Published references were detached into slots by ParameterPreparation.
                    assert isinstance(slot, ParameterSlot)  # nosec B101
                    if not slot.bind(parameters, parameter_value, always_parameterized):
                        return None
        for value, present, boundary_slot in (
            (limit, self.has_limit, self.limit_binding),
            (offset, self.has_offset, self.offset_binding),
        ):
            if value is PLAN_CACHE_MISS:
                continue
            if value is None:
                if present:
                    return None
            elif boundary_slot is None or not boundary_slot.positions or not boundary_slot.bind(parameters, value):
                return None
        return parameters
