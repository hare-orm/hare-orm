from __future__ import annotations

from collections.abc import Callable, Sequence
from enum import Enum
from functools import partial
from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.query.constants import PLAN_CACHE_MISS
from hare.query.expressions.value_refs.scalar_value_ref import ScalarValueRef
from hare.sql.queries.builder.query_builder import QueryBuilder
from hare.sql.queries.builder.set_operation_query import SetOperationQuery
from hare.sql.terms.array import Array
from hare.sql.terms.base.recording_parameterizer import RecordingParameterizer
from hare.sql.terms.base.term import Term
from hare.sql.terms.base.value_wrapper import ValueWrapper

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.fields.base.field import Field as ModelField
    from hare.models import Model
    from hare.query.expressions.value_refs.value_ref_types import ValueRef
    from hare.query.plans.recorded_join_condition import RecordedJoinCondition
    from hare.query.rows.hydration_layout import HydrationEntry


class StatementPlan:
    """How a query of one plan key runs: the SQL text built once, where each parameter comes from, and
    what reading the result needs - a later query of the key only binds its values.

    A parameter is a value bound through its recorded reference, the query's LIMIT or OFFSET, or a
    constant of the plan. A query whose text could differ from the plan's - a value rendered as a
    literal, a value of another type, a LIMIT or OFFSET on one side only, a value no reference binds
    - is built in full. The text isn't reused (``sql`` is None) when it holds a parameter that came
    from no term, or a CTE whose values the plan doesn't bind.
    """

    __slots__ = (
        "template",
        "value_refs",
        "sql",
        "parameters",
        "positions_by_source_id",
        "literal_source_ids",
        "value_types_by_source_id",
        "limit_source_id",
        "offset_source_id",
        "limit_binding",
        "offset_binding",
        "parameterizer",
        "result_reading",
        "scalar_bindings",
        "scalar_binders",
        "decode_plan",
        "decode_plan_key",
        "decode_plan_is_partial",
        "select_related_idx",
        "annotation_output_fields",
        "join_conditions",
    )

    #: The classes of values no reference binds - a term or an expression renders into the SQL
    #: text itself, a query is a subquery. Completed with the query classes once they are
    #: defined (they import this module).
    unbindable_value_classes: ClassVar[tuple[type, ...]] = (Term,)

    def __init__(
        self,
        template: QueryBuilder | SetOperationQuery,
        value_refs: tuple[tuple[str, ValueRef], ...],
        decode_plan: tuple[HydrationEntry, ...] | None = None,
        decode_plan_is_partial: bool = False,
        select_related_idx: tuple[Any, ...] = (),
        annotation_output_fields: tuple[tuple[str, ModelField[Any] | None], ...] = (),
        decode_plan_key: tuple[str | None, ...] | None = None,
        binds_ctes: bool = False,
        result_reading: Any = None,
        join_conditions: tuple[RecordedJoinCondition, ...] = (),
    ) -> None:
        """Renders ``template`` once, recording each parameter's source.

        Args:
            template: The query the first query of the key built.
            value_refs: The references its values were recorded with, in the order the query's
                description lists its values (``Plannable.get_plan_description()``).
            decode_plan: The row decode plan of a model query.
            decode_plan_is_partial: Whether the decode plan covers only some fields.
            select_related_idx: The ``select_related()`` row layout of a model query.
            annotation_output_fields: The output fields of the annotations whose results are
                decoded through a field.
            decode_plan_key: The names of the base model's selected columns a model query reads
                its rows by - what its executor is cached under.
            binds_ctes: Whether the values of the template's CTEs are among ``value_refs`` - the
                plan key then holds the structure of each CTE body.
            result_reading: What else a query of the key needs to read its result, kept for its
                type (the column converters of a set operation, say).
            join_conditions: The default scopes folded into its JOINs - their references follow
                the description's in ``value_refs``.
        """
        #: The built query - kept whole, the ids below are ids of its terms.
        self.template = template
        self.value_refs = value_refs
        parameterizer = RecordingParameterizer()
        # A set operation renders in the context of its first branch's query class.
        context_query = template.base_query if isinstance(template, SetOperationQuery) else template
        ctx = context_query.QUERY_CLS.SQL_CONTEXT.copy(parameterizer=parameterizer)
        sql = template.get_sql(ctx)
        #: Used for its should_parameterize() - the same decision rendering makes.
        self.parameterizer = parameterizer
        #: None when the text can't be reused - see the class docstring.
        self.sql: str | None = (
            sql
            if all(source is not None for source in parameterizer.sources) and (binds_ctes or not template._with)
            else None
        )
        #: The parameters as the first query bound them - a constant of the plan keeps its own.
        self.parameters: list[Any] = list(parameterizer.values)
        # By identity - a term's == builds a criterion instead of comparing.
        positions_by_source_id: dict[int, list[int]] = {}
        value_types_by_source_id: dict[int, type] = {}
        for position, source in enumerate(parameterizer.sources):
            positions_by_source_id.setdefault(id(source), []).append(position)
            value_types_by_source_id[id(source)] = self.get_value_type(source)
        self.positions_by_source_id = positions_by_source_id
        #: The terms the text holds as literals - a reference to one never binds.
        self.literal_source_ids = frozenset(id(source) for source in parameterizer.literal_sources)
        self.value_types_by_source_id = value_types_by_source_id
        self.limit_source_id = id(template._limit) if template._limit is not None else None
        self.offset_source_id = id(template._offset) if template._offset is not None else None
        #: The positions and the type of the LIMIT / OFFSET parameter, None when the text has
        #: none (no LIMIT / OFFSET, or one written into the text).
        self.limit_binding = self._get_slice_binding(self.limit_source_id)
        self.offset_binding = self._get_slice_binding(self.offset_source_id)
        self.scalar_bindings = self._get_scalar_bindings()
        self.result_reading = result_reading
        #: ``scalar_bindings`` with each field's conversion resolved for the dialect - built by
        #: the first ``bind()`` (a plan is kept per dialect).
        self.scalar_binders: (
            tuple[
                tuple[
                    Callable[[Any, Any], Any],
                    Callable[[Any, Any], Any] | None,
                    type | None,
                    tuple[int, ...],
                    type,
                    bool,
                    bool,
                ],
                ...,
            ]
            | None
        ) = None
        self.decode_plan = decode_plan
        self.decode_plan_key = decode_plan_key
        self.decode_plan_is_partial = decode_plan_is_partial
        self.select_related_idx = select_related_idx
        self.annotation_output_fields = annotation_output_fields
        self.join_conditions = join_conditions

    def get_join_condition_values(self) -> list[Any] | None:
        """The values of the default scopes folded into the plan's JOINs, as the current context
        makes them.

        Returns:
            The values, None when a scope has another structure now.
        """
        values: list[Any] = []
        for join_condition in self.join_conditions:
            join_condition_values = join_condition.get_values()
            if join_condition_values is None:
                return None
            values += join_condition_values
        return values

    def _get_scalar_bindings(self) -> tuple[tuple[ModelField[Any], tuple[int, ...], type], ...] | None:
        """What ``bind()`` needs of a plan whose every value is a plain ``field = value``
        comparison rendered as a parameter - the plan of most queries, bound without asking each
        reference for its parameters.

        Returns:
            Per value: its field, the positions of its parameter and the type of the recorded
            value. None for a plan with any other value.
        """
        scalar_bindings = []
        for _key, ref in self.value_refs:
            if type(ref) is not ScalarValueRef:
                return None
            positions = self.positions_by_source_id.get(id(ref.wrapper))
            if positions is None:
                return None
            scalar_bindings.append((ref.field, tuple(positions), self.value_types_by_source_id[id(ref.wrapper)]))
        return tuple(scalar_bindings)

    @property
    def query_builder(self) -> QueryBuilder:
        """The template of a plan of a query that isn't a set operation.

        Raises:
            TypeError: The plan is a set operation's.
        """
        template = self.template
        if not isinstance(template, QueryBuilder):
            raise TypeError("The plan of a set operation has no single query builder")
        return template

    def _get_slice_binding(self, source_id: int | None) -> tuple[tuple[int, ...], type] | None:
        """Where a query's LIMIT or OFFSET goes among the parameters.

        Args:
            source_id: The id of the plan's own LIMIT/OFFSET term, None when it has none.

        Returns:
            The positions of the parameter and the type of the recorded value, None when the
            text has no such parameter.
        """
        positions = self.positions_by_source_id.get(source_id) if source_id is not None else None
        if positions is None:
            return None
        return tuple(positions), self.value_types_by_source_id[cast("int", source_id)]

    def _get_scalar_binders(
        self, dialect: Dialect, scalar_bindings: tuple[tuple[ModelField[Any], tuple[int, ...], type], ...]
    ) -> tuple[
        tuple[
            Callable[[Any, Any], Any], Callable[[Any, Any], Any] | None, type | None, tuple[int, ...], type, bool, bool
        ],
        ...,
    ]:
        """``scalar_bindings`` ready to bind on ``dialect``.

        Args:
            dialect: The dialect the plan's queries run on.
            scalar_bindings: The plan's ``scalar_bindings``.

        Returns:
            Per value: the conversion the filter makes of a value (``TypeMap.get_lookup_value()``
            for its field, taking the value and the model), the native codec making it and the only
            value type it does (``HydrateAccelerator.get_lookup_writer()``), the positions of its parameter, the
            type of the recorded value, whether a value of that type may have to be written into
            the text instead (``Parameterizer.should_parameterize()``), and whether it is an enum
            member, bound as its value.
        """
        # Imported here: the accelerator's module imports the plans package.
        from hare.query.rows.hydrate_accelerator import HydrateAccelerator

        types = dialect.types
        conditionally_parameterized_types = self.parameterizer.conditionally_parameterized_types
        scalar_binders = []
        for field, positions, value_type in scalar_bindings:
            converter = types.get_lookup_converter(type(field))
            native_convert, native_value_type = HydrateAccelerator.get_lookup_writer(field, types) or (None, None)
            scalar_binders.append(
                (
                    field.to_lookup_value if converter is None else partial(converter, field),
                    native_convert,
                    native_value_type,
                    positions,
                    value_type,
                    issubclass(value_type, conditionally_parameterized_types),
                    issubclass(value_type, Enum),
                )
            )
        return tuple(scalar_binders)

    @staticmethod
    def get_value_type(source: Term | None) -> type:
        """The type of the value a term binds - the cast a parameter renders with can follow it.

        Args:
            source: The term.

        Returns:
            The type of its value.
        """
        if isinstance(source, ValueWrapper):
            return type(source.value)
        if isinstance(source, Array):
            return type(source.original_value)
        return type(source)

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
            values: The query's values, in the order of ``value_refs``.
            model: The model queried.
            dialect: The dialect the query runs on.
            limit: The query's LIMIT (None for none); ``PLAN_CACHE_MISS`` keeps the plan's own.
            offset: The query's OFFSET, the same way.

        Returns:
            The parameters, or None when the query's SQL text would differ from the plan's.
        """
        if self.sql is None or len(values) != len(self.value_refs):
            return None
        parameters = self.parameters.copy()
        unbindable_value_classes = self.unbindable_value_classes
        should_parameterize = self.parameterizer.should_parameterize
        scalar_bindings = self.scalar_bindings
        if scalar_bindings is not None:
            scalar_binders = self.scalar_binders
            if scalar_binders is None:
                scalar_binders = self.scalar_binders = self._get_scalar_binders(dialect, scalar_bindings)
            for (
                convert,
                native_convert,
                native_value_type,
                positions,
                value_type,
                is_conditionally_parameterized,
                is_enum,
            ), value in zip(scalar_binders, values, strict=True):
                if value is None or isinstance(value, unbindable_value_classes):
                    return None
                # The conversion the filter made of the recorded value; a value converted to a
                # term (an IntField's cast for a float) has another type than the recorded one.
                parameter_value = (
                    native_convert(value, model)
                    if native_convert is not None and (native_value_type is None or type(value) is native_value_type)
                    else convert(value, model)
                )
                if type(parameter_value) is not value_type:
                    return None
                if is_conditionally_parameterized and not should_parameterize(parameter_value):
                    return None
                if is_enum:
                    while isinstance(parameter_value, Enum):
                        parameter_value = parameter_value.value
                for position in positions:
                    parameters[position] = parameter_value
        else:
            positions_by_source_id = self.positions_by_source_id
            value_types_by_source_id = self.value_types_by_source_id
            for (_key, ref), value in zip(self.value_refs, values, strict=True):
                if value is None or isinstance(value, unbindable_value_classes):
                    return None
                parameter_values = ref.get_parameter_values(value, model, dialect)
                if parameter_values is None:
                    return None
                for source_id, parameter_value, always_parameterized in parameter_values:
                    source_positions = positions_by_source_id.get(source_id)
                    if source_positions is None:
                        # A term the text doesn't hold has no say in the statement.
                        if source_id in self.literal_source_ids:
                            return None
                        continue
                    if type(parameter_value) is not value_types_by_source_id[source_id]:
                        return None
                    if not always_parameterized and not should_parameterize(parameter_value):
                        return None
                    while isinstance(parameter_value, Enum):
                        parameter_value = parameter_value.value
                    for position in source_positions:
                        parameters[position] = parameter_value
        # A query's LIMIT and OFFSET. The SQL text would differ when one side has the bound and
        # the other not, the bound isn't a parameter of the text, or its type differs.
        if limit is not PLAN_CACHE_MISS:
            if limit is None:
                if self.limit_source_id is not None:
                    return None
            else:
                limit_binding = self.limit_binding
                if limit_binding is None or type(limit) is not limit_binding[1]:
                    return None
                for position in limit_binding[0]:
                    parameters[position] = limit
        if offset is not PLAN_CACHE_MISS:
            if offset is None:
                if self.offset_source_id is not None:
                    return None
            else:
                offset_binding = self.offset_binding
                if offset_binding is None or type(offset) is not offset_binding[1]:
                    return None
                for position in offset_binding[0]:
                    parameters[position] = offset
        return parameters
