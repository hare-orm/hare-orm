from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from hare.exceptions import FieldError
from hare.query.key_columns import KeyColumns
from hare.query.queryset.concrete_field_paths import ConcreteFieldPaths
from hare.query.rows.native.enums import FieldBucket
from hare.query.rows.values_rows.value_field import ValueField
from hare.query.statements.building.query_annotations import QueryAnnotations
from hare.query.statements.building.query_joins import QueryJoins

TModel = TypeVar("TModel", bound="Model")

if TYPE_CHECKING:
    from hare.fields.field import Field as ModelField
    from hare.models import Model
    from hare.query.statements.select.values_query import ValuesQuery


class ValuesOutput:
    """What a row of values() holds: the selected names and the aliases they are selected under, the
    field each value is read through, and the converters and readers turning a fetched row into the
    returned one."""

    @staticmethod
    def get_composite_key_components(
        model: type[Model], name: str, annotations: dict[str, Any], alias_keys: Collection[str] = ()
    ) -> tuple[str, ...]:
        """The field paths of a selected name reading a composite key - ``pk`` of a composite
        primary key, or a relation to one (``target``, ``target__pk``, ``tags``).

        Args:
            model: The queried model.
            name: The selected name.
            annotations: The query's annotations.
            alias_keys: Names used as aliases rather than scalar annotations.

        Returns:
            The key's field paths in key order, empty when the name reads one column.
        """
        if annotations:
            expanded = KeyColumns.get_primary_key_name(model, annotations, name)
            if name not in annotations or name in alias_keys or expanded == "pk":
                name = expanded
        if name in annotations:
            return ()
        field_paths = ConcreteFieldPaths.get_paths(model, name)
        return field_paths if len(field_paths) > 1 else ()

    @staticmethod
    def get_value_fields(query: ValuesQuery) -> tuple[ValueField, ...]:
        """What each selected column is read as.

        Args:
            query: The values query.

        Returns:
            The value fields, in output order.
        """
        return tuple(
            ValuesOutput.get_selected_value_field(query, query.model, name)
            for name in ValuesOutput.get_output_field_names(query)
        )

    @staticmethod
    def get_column_converters(
        query: ValuesQuery, value_fields: tuple[ValueField, ...] | None = None
    ) -> list[tuple[str, Callable[[Any], Any] | None]]:
        """Each selected column's alias and the function decoding its value, None when the raw
        value is used as-is.

        Args:
            query: The values query.
            value_fields: What each column is read as, when already known.

        Returns:
            The converters, in output order.
        """
        if value_fields is None:
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
        return ConcreteFieldPaths.get_path(query.model, field)

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
            # A dict of its own: the annotations are the originating queryset's.
            query._annotations = {**query._annotations, return_as: query._annotations[field]}
            return

        if field in query.model._meta.fields_db_projection:
            db_field = query.model._meta.fields_db_projection[field]
            query.query._select_field(table[db_field].as_(return_as))
            return

        field_, __, forwarded_fields = field.partition("__")
        if field_ in query.model._meta.fetch_fields:
            related_table, related_db_field = QueryJoins.join_table_with_forwarded_fields(
                query,
                model=query.model,
                table=table,
                field=field_,
                forwarded_fields=forwarded_fields,
            )
            query.query._select_field(related_table[related_db_field].as_(return_as))
            return

        raise FieldError(f"Unknown field {field!r} for model {query.model.__name__!r}")

    @staticmethod
    def get_selected_value_field(
        query: ValuesQuery, model: type[TModel], field: str, *, is_root: bool = True
    ) -> ValueField:
        """What a selected name's value is read as.

        Args:
            query: The values query.
            model: The model the name is a path from.
            field: The name.
            is_root: Whether the name is the selected one, not its rest past a relation - only the selected
                name can be one of the query's annotations.

        Returns:
            The model and name of a model field (the model None for an annotation), with the field
            the value is decoded through - None for a value used as the driver returns it.

        Raises:
            FieldError: The name is neither a field nor an annotation.
        """
        if is_root and model is query.model:
            field = ValuesOutput.get_concrete_field_path(query, field)

        if field in model._meta.fetch_fields:
            # return as is to get whole model objects
            return ValueField(None, field, None, is_native=True)

        layout_entry = model._meta.get_hydration_layout(query._connection).entry_by_field_name.get(field)
        if layout_entry is not None and layout_entry[2] == FieldBucket.NATIVE:
            return ValueField(model, field, layout_entry[1], is_native=True)

        if is_root and field in query._annotations:
            # Read from this build's own dict - the annotation expression is shared between queries.
            field_object = query._annotation_output_fields.get(field)
            return ValueField(None, field, field_object or None, is_native=not field_object)

        if field in model._meta.fields_map:
            return ValueField(model, field, model._meta.fields_map[field], is_native=False)

        field_, __, forwarded_fields = field.partition("__")
        if field_ in model._meta.fetch_fields:
            new_model = model._meta.fields_map[field_].related_model  # type: ignore[attr-defined]
            return ValuesOutput.get_selected_value_field(query, new_model, forwarded_fields, is_root=False)

        raise FieldError(f"Unknown field {field!r} for model {model.__name__!r}")

    @staticmethod
    def register_selected_annotations(query: ValuesQuery, output_aliases: Mapping[str, str]) -> None:
        """Registers each selected annotation again under its alias - the part of
        ``add_field_to_select_query()`` that builds nothing, in the same order, so the query can be
        described before the selected fields are resolved.

        Args:
            query: The values query.
            output_aliases: Each selected field name by its alias.
        """
        for alias, field in output_aliases.items():
            field = ValuesOutput.get_concrete_field_path(query, field)
            if field in query._annotations:
                query._annotations = {**query._annotations, alias: query._annotations[field]}

    @staticmethod
    def get_output_field_names(query: ValuesQuery) -> Collection[str]:
        """The field/annotation names visible to the caller, one per selected column.

        Args:
            query: The values query.
        """
        return query._selected_fields_by_alias.values()

    @staticmethod
    def selects_composite_key(query: ValuesQuery) -> bool:
        """Whether a selected name returns a composite key, combined from several columns.

        Args:
            query: The values query.
        """
        return query._rows.has_composite_outputs

    @staticmethod
    def get_output_aliases(query: ValuesQuery) -> Collection[str]:
        """The SELECT aliases of the caller-visible columns, in output order.

        Args:
            query: The values query.
        """
        return query._selected_fields_by_alias.keys()

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
    def get_selected_fields_by_alias(query: ValuesQuery) -> Mapping[str, str]:
        """Each selected field or annotation name by the alias its column is selected under.

        Args:
            query: The values query.
        """
        return query._selected_fields_by_alias

    @staticmethod
    def get_ordering_field_names(query: ValuesQuery, annotation_output_aliases: Mapping[str, str]) -> Collection[str]:
        """The selected names ``QueryOrdering.get_ordering()`` may order by.

        Args:
            query: The values query.
            annotation_output_aliases: Each selected annotation's name -> the alias it is SELECTed
                under.

        Returns:
            The names.
        """
        if query._rows.keyed_by_output_name:
            # A renamed annotation (.values(renamed="annotation")) is SELECTed under its new name.
            return [*query._selected_fields_by_alias, *annotation_output_aliases]
        return query._selected_fields_by_alias.values()

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
