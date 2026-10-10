from __future__ import annotations

from collections.abc import Callable, Sequence
from enum import Enum
from functools import partial
from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.query.constants import PLAN_CACHE_MISS
from hare.query.expressions.value_references.scalar_value_reference import ScalarValueReference
from hare.query.expressions.value_references.write_value_reference import WriteValueReference
from hare.sql.builder.queries.query_builder import QueryBuilder
from hare.sql.builder.queries.set_operation_query import SetOperationQuery
from hare.sql.terms.array import Array
from hare.sql.terms.parameters.recording_parameterizer import RecordingParameterizer
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.fields.field import Field as ModelField
    from hare.models import Model
    from hare.query.expressions.value_references.value_reference_types import ValueReference
    from hare.query.plans.recording.scope_value_source import ScopeValueSource
    from hare.query.rows.native.hydration_layout import HydrationEntry


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
        "template",
        "value_sources",
        "repeated_values",
        "sql",
        "parameters",
        "positions_by_source_id",
        "literal_source_ids",
        "value_types_by_source_id",
        "conditionally_parameterized_source_ids",
        "enum_source_ids",
        "limit_source_id",
        "offset_source_id",
        "limit_binding",
        "offset_binding",
        "parameterizer",
        "result_reading",
        "scalar_bindings",
        "compiled_bind",
        "decode_plan",
        "decode_plan_key",
        "decode_plan_is_partial",
        "model_readers",
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
                its rows by - what its executor is cached under.
            binds_ctes: Whether the values of the template's CTEs are among ``value_sources`` - the
                plan key then holds the structure of each CTE body.
            result_reading: What else a query of the key needs to read its result, kept for its
                type (the column converters of a set operation, say).
            scope_sources: The default scopes folded into its JOINs - their references follow
                the description's in ``value_sources``.
        """
        #: The built query - kept whole, the ids below are ids of its terms.
        self.template = template
        self.value_sources = value_sources
        self.repeated_values = repeated_values
        parameterizer = RecordingParameterizer()
        # A set operation renders in the context of its first branch's query class.
        context_query = template.base_query if isinstance(template, SetOperationQuery) else template
        sql_context = context_query.query_class.SQL_CONTEXT.copy(parameterizer=parameterizer)
        sql = template.get_sql(sql_context)
        #: Used for its should_parameterize() - the same decision rendering makes.
        self.parameterizer = parameterizer
        #: None when the text can't be reused - see the class docstring.
        self.sql: str | None = (
            sql
            if all(source is not None for source in parameterizer.sources) and (binds_ctes or not template._with)
            else None
        )
        #: The parameters as the first query bound them - a constant of the plan keeps its own. A
        #: ``QueryParameters`` keeps its hidden positions in every copy ``bind()`` makes.
        self.parameters: list[Any] = parameterizer.values.copy()
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
        conditionally_parameterized_types = parameterizer.conditionally_parameterized_types
        #: The terms whose recorded value is of a type ``should_parameterize()`` may refuse - a value
        #: binding through one is checked again.
        self.conditionally_parameterized_source_ids = frozenset(
            source_id
            for source_id, value_type in value_types_by_source_id.items()
            if issubclass(value_type, conditionally_parameterized_types)
        )
        #: The terms whose recorded value is an enum member - a value binding through one binds as
        #: its value.
        self.enum_source_ids = frozenset(
            source_id for source_id, value_type in value_types_by_source_id.items() if issubclass(value_type, Enum)
        )
        self.limit_source_id = id(template._limit) if template._limit is not None else None
        self.offset_source_id = id(template._offset) if template._offset is not None else None
        #: The positions and the type of the LIMIT / OFFSET parameter, None when the text has
        #: none (no LIMIT / OFFSET, or one written into the text).
        self.limit_binding = self._get_slice_binding(self.limit_source_id)
        self.offset_binding = self._get_slice_binding(self.offset_source_id)
        self.scalar_bindings = self._get_scalar_bindings()
        self.result_reading = result_reading
        self.decode_plan = decode_plan
        self.decode_plan_key = decode_plan_key
        self.decode_plan_is_partial = decode_plan_is_partial
        #: The native reader of the decode plan's rows per zone aware datetimes are read in - made by
        #: the first run on the plan (a plan is kept per dialect).
        self.model_readers: dict[str | None, Any] = {}
        #: ``bind()`` of a plan of scalar values alone, compiled by its first call - see
        #: ``_compile_scalar_bind()``.
        self.compiled_bind: Callable[[Sequence[Any], Any, Any, Any], list[Any] | None] | None = None
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

    def _get_scalar_bindings(self) -> tuple[tuple[ModelField[Any], tuple[int, ...], type, bool], ...] | None:
        """What ``bind()`` needs of a plan whose every value is a plain ``field = value``
        comparison or ``SET`` assignment rendered as a parameter - each time the build resolved it -
        the plan of most queries, bound without asking each reference for its parameters.

        Returns:
            Per value: its field, the positions of its parameters, the type of the recorded value
            and whether it is a written value (``WriteValueReference``). None for a plan with any
            other value, or a value listed again.
        """
        if self.repeated_values:
            return None
        positions_by_source_id = self.positions_by_source_id
        value_types_by_source_id = self.value_types_by_source_id
        scalar_bindings = []
        for references in self.value_sources:
            if not references:
                return None
            reference_class = type(references[0])
            if reference_class is not ScalarValueReference and reference_class is not WriteValueReference:
                return None
            field = cast("ScalarValueReference | WriteValueReference", references[0]).field
            value_type = None
            positions: list[int] = []
            for reference in references:
                if type(reference) is not reference_class:
                    return None
                wrapper_reference = cast("ScalarValueReference | WriteValueReference", reference)
                if wrapper_reference.field is not field:
                    return None
                source_positions = positions_by_source_id.get(id(wrapper_reference.wrapper))
                if source_positions is None:
                    return None
                source_value_type = value_types_by_source_id[id(wrapper_reference.wrapper)]
                if value_type is None:
                    value_type = source_value_type
                elif source_value_type is not value_type:
                    return None
                positions += source_positions
            scalar_bindings.append((field, tuple(positions), value_type, reference_class is WriteValueReference))
        return tuple(scalar_bindings)  # type: ignore[arg-type]

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
        self, dialect: Dialect, scalar_bindings: tuple[tuple[ModelField[Any], tuple[int, ...], type, bool], ...]
    ) -> tuple[
        tuple[
            Callable[[Any, Any], Any],
            Callable[[Any, Any], Any] | None,
            type | None,
            tuple[int, ...],
            type,
            bool,
            bool,
            bool,
        ],
        ...,
    ]:
        """``scalar_bindings`` ready to bind on ``dialect``.

        Args:
            dialect: The dialect the plan's queries run on.
            scalar_bindings: The plan's ``scalar_bindings``.

        Returns:
            Per value: the conversion the filter makes of a value (``TypeRegistry.get_lookup_value()``
            for its field, taking the value and the model) or the write makes of it
            (``TypeRegistry.get_db_value()``, taking the value and None), the native codec making a
            filter's and the only value type it does (``HydrateAccelerator.get_lookup_writer()``),
            the positions of its parameter, the type of the recorded value, whether a value of that
            type may have to be written into the text instead
            (``Parameterizer.should_parameterize()``), whether it is an enum member, bound as its
            value, and whether it is a written value.
        """
        # Imported here: the accelerator's module imports the plans package.
        from hare.query.rows.native.hydrate_accelerator import HydrateAccelerator

        types = dialect.types
        conditionally_parameterized_types = self.parameterizer.conditionally_parameterized_types
        scalar_binders = []
        for field, positions, value_type, written in scalar_bindings:
            if written:
                converter = types.get_db_converter(type(field))
                convert = field.to_db_value if converter is None else partial(converter, field)
                native_convert, native_value_type = None, None
            else:
                converter = types.get_lookup_converter(type(field))
                convert = field.to_lookup_value if converter is None else partial(converter, field)
                native_convert, native_value_type = HydrateAccelerator.get_lookup_writer(field, types) or (None, None)
            scalar_binders.append(
                (
                    convert,
                    native_convert,
                    native_value_type,
                    positions,
                    value_type,
                    issubclass(value_type, conditionally_parameterized_types),
                    issubclass(value_type, Enum),
                    written,
                )
            )
        return tuple(scalar_binders)

    def _compile_scalar_bind(
        self, dialect: Dialect, scalar_bindings: tuple[tuple[ModelField[Any], tuple[int, ...], type, bool], ...]
    ) -> Callable[[Sequence[Any], Any, Any, Any], list[Any] | None]:
        """``bind()`` of a plan of scalar values alone, as one function with no loop: each value
        converted by its field's conversion, then the LIMIT and OFFSET - built by the plan's first
        ``bind()`` (a plan is kept per dialect).

        Args:
            dialect: The dialect the plan's queries run on.
            scalar_bindings: The plan's ``scalar_bindings``.

        Returns:
            The function, taking the values, the model, the LIMIT and the OFFSET.
        """
        namespace: dict[str, Any] = {
            "base_parameters": self.parameters,
            "unbindable_value_classes": self.unbindable_value_classes,
            "should_parameterize": self.parameterizer.should_parameterize,
            "Enum": Enum,
            "MISS": PLAN_CACHE_MISS,
        }
        lines = [f"    if len(values) != {len(scalar_bindings)}:", "        return None"]
        for position, first_position in self.repeated_values:
            lines += [f"    if values[{position}] is not values[{first_position}]:", "        return None"]
        lines.append("    parameters = base_parameters.copy()")
        for index, binder in enumerate(self._get_scalar_binders(dialect, scalar_bindings)):
            convert, native_convert, native_value_type, positions, value_type, is_conditional, is_enum, written = (
                binder
            )
            namespace.update(
                {
                    f"convert_{index}": convert,
                    f"native_convert_{index}": native_convert,
                    f"native_value_type_{index}": native_value_type,
                    f"value_type_{index}": value_type,
                }
            )
            lines += [
                f"    value = values[{index}]",
                "    if value is None or isinstance(value, unbindable_value_classes):",
                "        return None",
            ]
            # The conversion the filter or the write made of the recorded value; a value converted
            # to a term (an IntField's cast for a float) has another type than the recorded one. A
            # written value is converted for no instance.
            if written:
                lines.append(f"    parameter_value = convert_{index}(value, None)")
            elif native_convert is None:
                lines.append(f"    parameter_value = convert_{index}(value, model)")
            elif native_value_type is None:
                lines.append(f"    parameter_value = native_convert_{index}(value, model)")
            else:
                lines.append(
                    f"    parameter_value = native_convert_{index}(value, model) "
                    f"if type(value) is native_value_type_{index} else convert_{index}(value, model)"
                )
            lines += [f"    if type(parameter_value) is not value_type_{index}:", "        return None"]
            if is_conditional:
                lines += ["    if not should_parameterize(parameter_value):", "        return None"]
            if is_enum:
                lines += [
                    "    while isinstance(parameter_value, Enum):",
                    "        parameter_value = parameter_value.value",
                ]
            lines += [f"    parameters[{position}] = parameter_value" for position in positions]
        lines += self._get_slice_bind_source("limit", self.limit_source_id, self.limit_binding, namespace)
        lines += self._get_slice_bind_source("offset", self.offset_source_id, self.offset_binding, namespace)
        lines.append("    return parameters")
        source = "def bind(values, model, limit, offset):\n" + "\n".join(lines)
        exec(source, namespace)  # nosec B102 - plan-derived source, no external input
        return cast("Callable[[Sequence[Any], Any, Any, Any], list[Any] | None]", namespace["bind"])

    @staticmethod
    def _get_slice_bind_source(
        name: str, source_id: int | None, binding: tuple[tuple[int, ...], type] | None, namespace: dict[str, Any]
    ) -> list[str]:
        """The lines of a compiled ``bind()`` binding the LIMIT or the OFFSET - the SQL text would
        differ when one side has the bound and the other not, the bound isn't a parameter of the
        text, or its type differs.

        Args:
            name: ``limit`` or ``offset`` - the argument of the compiled function.
            source_id: The plan's source of the bound, None for none.
            binding: The positions and type of its parameter, None when the text has none.
            namespace: The namespace of the compiled function - gets the type of the bound.

        Returns:
            The lines.
        """
        lines = [f"    if {name} is not MISS:", f"        if {name} is None:"]
        lines.append("            return None" if source_id is not None else "            pass")
        if binding is None:
            lines += ["        else:", "            return None"]
            return lines
        namespace[f"{name}_type"] = binding[1]
        lines += ["        else:", f"            if type({name}) is not {name}_type:", "                return None"]
        lines += [f"            parameters[{position}] = {name}" for position in binding[0]]
        return lines

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
            values: The query's values, in the order of ``value_sources``.
            model: The model queried.
            dialect: The dialect the query runs on.
            limit: The query's LIMIT (None for none); ``PLAN_CACHE_MISS`` keeps the plan's own.
            offset: The query's OFFSET, the same way.

        Returns:
            The parameters, or None when the query's SQL text would differ from the plan's.
        """
        compiled_bind = self.compiled_bind
        if compiled_bind is not None:
            return compiled_bind(values, model, limit, offset)
        if self.sql is None or len(values) != len(self.value_sources):
            return None
        if self.scalar_bindings is not None:
            self.compiled_bind = self._compile_scalar_bind(dialect, self.scalar_bindings)
            return self.compiled_bind(values, model, limit, offset)
        for position, first_position in self.repeated_values:
            if values[position] is not values[first_position]:
                return None
        parameters = self.parameters.copy()
        unbindable_value_classes = self.unbindable_value_classes
        should_parameterize = self.parameterizer.should_parameterize
        positions_by_source_id = self.positions_by_source_id
        value_types_by_source_id = self.value_types_by_source_id
        conditionally_parameterized_source_ids = self.conditionally_parameterized_source_ids
        enum_source_ids = self.enum_source_ids
        for references, value in zip(self.value_sources, values, strict=True):
            if value is None or isinstance(value, unbindable_value_classes):
                return None
            for reference in references:
                parameter_values = reference.get_parameter_values(value, model, dialect)
                if parameter_values is None:
                    return None
                for source_id, parameter_value, always_parameterized in parameter_values:
                    source_positions = positions_by_source_id.get(source_id)
                    if source_positions is None:
                        # A term the text doesn't hold has no say in the statement.
                        if source_id in self.literal_source_ids:
                            return None
                        continue
                    # Of the recorded type - whether rendering may refuse to bind it, and whether it
                    # is an enum member, the type decides.
                    if type(parameter_value) is not value_types_by_source_id[source_id]:
                        return None
                    if (
                        not always_parameterized
                        and source_id in conditionally_parameterized_source_ids
                        and not should_parameterize(parameter_value)
                    ):
                        return None
                    if source_id in enum_source_ids:
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
