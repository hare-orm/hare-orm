from __future__ import annotations

from collections.abc import Callable, Collection
from typing import TYPE_CHECKING, Any, TypeVar

from hare.exceptions import FieldError
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.key_columns import KeyColumns
from hare.query.lookup_info.bound_lookup_path import BoundLookupPath
from hare.query.queryset.concrete_field_paths import ConcreteFieldPaths
from hare.query.rows.values_rows.value_field import ValueField
from hare.query.statements.building.query_annotations import QueryAnnotations
from hare.query.statements.building.query_joins import QueryJoins

TModel = TypeVar("TModel", bound="Model")

if TYPE_CHECKING:
    from hare.fields.field import Field as ModelField
    from hare.models import Model
    from hare.models.model_binding import ModelBinding
    from hare.query.statements.select.values_query import ValuesQuery


class ValuesOutput:
    """What a row of values() holds: the selected names and the aliases they are selected under, the
    field each value is read through, and the converters and readers turning a fetched row into the
    returned one."""

    @staticmethod
    def get_composite_key_components(binding: ModelBinding, name: str, annotations: dict[str, Any]) -> tuple[str, ...]:
        """The field paths of a selected name reading a composite key - ``pk`` of a composite
        primary key, a relation to one (``target``, ``target__pk``, ``tags``), or an ``F`` annotation
        naming such a key.

        Args:
            binding: The queried model.
            name: The selected name.
            annotations: The query's annotations.

        Returns:
            The key's field paths in key order, empty when the name reads one column.
        """
        if annotations:
            name = KeyColumns.get_primary_key_name(binding, annotations, name)
        if name in annotations:
            return ()
        field_paths = ConcreteFieldPaths.get_paths(binding, name)
        return field_paths if len(field_paths) > 1 else ()

    @staticmethod
    def get_value_fields(query: ValuesQuery) -> tuple[ValueField, ...]:
        """What each selected column is read as.

        Args:
            query: The values query.

        Returns:
            The value fields, in output order.
        """
        model_binding: ModelBinding = query._binding  # type: ignore[assignment]
        return tuple(
            ValuesOutput.get_selected_value_field(query, model_binding, name)
            for name in query._get_selected_field_names()
        )

    @staticmethod
    def get_column_converters(
        query: ValuesQuery,
    ) -> list[tuple[str, Callable[[Any], Any] | None]]:
        """Each selected column's alias and the function decoding its value, None when the raw
        value is used as-is.

        Args:
            query: The values query.

        Returns:
            The converters, in output order.
        """
        value_fields = ValuesOutput.get_value_fields(query)
        python_reader = query.dialect.types.get_python_reader
        return [
            (alias, None if value_field.is_native or value_field.field is None else python_reader(value_field.field))
            for alias, value_field in zip(ValuesOutput.get_output_aliases(query), value_fields, strict=True)
        ]

    @staticmethod
    def get_output_value_field(query: ValuesQuery, field_name: str) -> ModelField[Any] | None:
        """The field a selected field or annotation decodes through, once the query is built.

        Args:
            query: The values query.
            field_name: The selected name.

        Returns:
            The field, or None when unknown.
        """
        from hare.query.functions.aggregates.count import Count

        field_name = ValuesOutput.get_concrete_field_path(query, field_name)
        if field_name in query._annotations:
            if (output_field := query._annotation_output_fields.get(field_name)) is not None:
                return output_field
            if isinstance(query._annotations[field_name], Count):
                return Count.COUNT_OUTPUT_FIELD
            return None
        return QueryAnnotations.get_field_object_by_path(query, field_name)

    @staticmethod
    def get_concrete_field_path(query: ValuesQuery, field: str) -> str:
        """The field path a selected name reads - an annotation name itself, else a trailing
        ``pk`` or relation translated like ``ConcreteFieldPaths.get_path()`` does
        (``pk`` -> ``id``, ``dept`` -> ``dept_id``, ``tags`` -> ``tags__id``).

        Args:
            query: The values query.
            field: The selected name.

        Returns:
            The field path or annotation name.

        Raises:
            QueryError: The name reads a composite primary key or foreign key.
        """
        if field in query._annotations:
            return field
        model_binding: ModelBinding = query._binding  # type: ignore[assignment]
        return ConcreteFieldPaths.get_path(model_binding, field)

    @staticmethod
    def add_field_to_select_query(query: ValuesQuery, field: str, return_as: str) -> None:
        """Selects a field, a path or an annotation of a ``values()`` query under a name.

        Args:
            query: The query.
            field: The field, path or annotation.
            return_as: The name of the value in a row.
        """
        field = ValuesOutput.get_concrete_field_path(query, field)
        table = query._effective_basetable()

        if field in query._annotations:
            # QueryAnnotations selects this source expression under its output aliases.
            return

        model_binding: ModelBinding = query._binding  # type: ignore[assignment]
        if field in model_binding.fields_db_projection:
            db_field = model_binding.fields_db_projection[field]
            query.query._select_field(table[db_field].as_(return_as))
            return

        if field.partition("__")[0] in model_binding.fetch_fields:
            context = ExpressionContext(
                binding=model_binding,
                table=table,
                annotations=query._annotations,
                dialect=query.dialect,
                connection=query._connection,
                visibility=query._visibility,
                select_related_extra_conditions=query._select_related_extra_conditions,
                application_context=query._context,
            )
            bound_path = BoundLookupPath.bind(context, field)
            for join in bound_path.joins:
                QueryJoins.join_table(query, join)
            query.query._select_field(bound_path.column.as_(return_as))
            return

        raise FieldError(f"Unknown field {field!r} for model {query.model.__name__!r}")

    @staticmethod
    def get_selected_value_field(
        query: ValuesQuery, binding: ModelBinding, field: str, *, is_root: bool = True
    ) -> ValueField:
        """What a selected name's value is read as.

        Args:
            query: The values query.
            binding: The model the name is a path from.
            field: The name.
            is_root: Whether the name is the selected one, not its rest past a relation - only the selected
                name can be one of the query's annotations.

        Returns:
            The value field and whether to preserve the driver's value without conversion.
            A missing field leaves the driver value unchanged.

        Raises:
            FieldError: The name is neither a field nor an annotation.
        """
        model = binding._model
        if is_root and model is query.model:
            field = ValuesOutput.get_concrete_field_path(query, field)

        if field in binding.fetch_fields:
            # return as is to get whole model objects
            return ValueField(None, is_native=True)

        layout_entry = binding.get_hydration_layout(query._connection).entry_by_field_name.get(field)
        if layout_entry is not None and layout_entry[2] is None:
            return ValueField(layout_entry[1], is_native=True)

        if is_root and field in query._annotations:
            # Read from this build's own dict - the annotation expression is shared between queries.
            field_object = query._annotation_output_fields.get(field)
            return ValueField(field_object or None, is_native=not field_object)

        if field in binding.fields_map:
            return ValueField(binding.fields_map[field], is_native=False)

        field_, __, forwarded_fields = field.partition("__")
        if field_ in binding.fetch_fields:
            return ValuesOutput.get_selected_value_field(
                query,
                binding.fields_map[field_].get_related_binding(),  # type: ignore[attr-defined]
                forwarded_fields,
                is_root=False,
            )

        raise FieldError(f"Unknown field {field!r} for model {model.__name__!r}")

    @staticmethod
    def get_output_aliases(query: ValuesQuery) -> Collection[str]:
        """The SELECT aliases of the caller-visible columns, in output order.

        Args:
            query: The values query.
        """
        aliases = query._result_column_aliases
        return (
            query._selected_fields_by_alias.keys()
            if aliases is None
            else tuple(aliases.get(name, name) for name in query._selected_fields_by_alias)
        )

    @staticmethod
    def get_output_names_for_set_operation(query: ValuesQuery) -> list[str]:
        """The names a set operation's rows are ordered by - the dict keys, or the selected
        names of a ``values_list()``.

        Args:
            query: The values query.

        Returns:
            The names, in output order.
        """
        if query._rows.keyed_by_output_name:
            return list(query._selected_fields_by_alias)
        return list(query._selected_fields_by_alias.values())

    @staticmethod
    def get_field_by_selected_name(query: ValuesQuery, selected_name: str) -> ModelField[Any] | None:
        """The field of a selected column, with its alias taking precedence over a field name."""
        field_name = query._selected_fields_by_alias.get(selected_name, selected_name)
        return ValuesOutput.get_output_value_field(query, field_name)

    @staticmethod
    def get_output_reader(query: ValuesQuery, field_name: str) -> Callable[[Any], Any] | None:
        """Reads the value of a selected field off one output row.

        Args:
            query: The values query.
            field_name: The field name.

        Returns:
            The reader, or None when the field isn't selected on its own.
        """
        for position, (alias, selected_name) in enumerate(query._selected_fields_by_alias.items()):
            if ValuesOutput.get_concrete_field_path(query, selected_name) == field_name:
                return query._rows.get_reader(position, alias)
        return None
