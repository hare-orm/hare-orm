from __future__ import annotations

import functools
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ClassVar

from hare.core.cache import Cache
from hare.core.registries import Registries

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.base.field import Field
    from hare.fields.generated import GeneratedField
    from hare.models import Model
    from hare.sql.terms.base.term import Term

    #: ``(field, value, instance) -> value``; the field parameter is typed by each converter.
    DbConverter = Callable[..., Any]
    #: ``(field, value) -> value``; the field parameter is typed by each converter.
    PythonConverter = Callable[..., Any]
from hare.dialects.base.types.type_mapping import TypeMapping


class TypeRegistry:
    """The type mappings of one dialect, found through a field's class hierarchy - a field class
    of its own uses the mapping of the nearest registered base class."""

    #: What each registry found for a field class through the class's bases, kept in a bucket of
    #: the registry: its mappings, nearest first, and its ``to_db``, ``to_lookup`` and ``to_python``.
    found_mappings: ClassVar[Cache[Any]] = Cache(holds_sql=False, keyed_by_model=False)
    found_db_converters: ClassVar[Cache[Any]] = Cache(holds_sql=False, keyed_by_model=False)
    found_lookup_converters: ClassVar[Cache[Any]] = Cache(holds_sql=False, keyed_by_model=False)
    found_python_converters: ClassVar[Cache[Any]] = Cache(holds_sql=False, keyed_by_model=False)

    def __init__(self) -> None:
        from hare.fields.generated import GeneratedField

        #: The buckets of the caches this registry reads (``Cache.get_owner_bucket()``).
        self.cache_buckets: dict[int, Any] = {}
        self.mappings: dict[type, TypeMapping] = {}
        self.mappings_by_field_class: dict[type, tuple[TypeMapping, ...]] = (
            TypeRegistry.found_mappings.get_owner_bucket(self)
        )
        self.db_converters: dict[type, DbConverter | None] = TypeRegistry.found_db_converters.get_owner_bucket(self)
        self.lookup_converters: dict[type, DbConverter | None] = TypeRegistry.found_lookup_converters.get_owner_bucket(
            self
        )
        self.python_converters: dict[type, PythonConverter | None] = (
            TypeRegistry.found_python_converters.get_owner_bucket(self)
        )
        #: Whether a dialect reads its fields through this registry - until then nothing has
        #: cached what it maps, so a mapping registered while it is built drops no cache.
        self.in_use = False
        #: The Python types bound as text - a datetime as the ISO text of its UTC instant, a date
        #: and a time as their ISO text, a Decimal in fixed-point notation.
        self.bound_as_text: frozenset[type] = frozenset()
        self.register(
            GeneratedField,
            TypeMapping(
                to_db=self.get_generated_db_value,
                to_lookup=self.get_generated_lookup_value,
                to_python=self.get_generated_python_value,
            ),
        )

    def register(self, field_class: type, mapping: TypeMapping) -> None:
        """Sets how the dialect stores ``field_class`` and its subclasses.

        Args:
            field_class: The field class.
            mapping: Its mapping.
        """
        self.mappings[field_class] = mapping
        TypeRegistry.found_mappings.forget_owner(self)
        TypeRegistry.found_db_converters.forget_owner(self)
        TypeRegistry.found_lookup_converters.forget_owner(self)
        TypeRegistry.found_python_converters.forget_owner(self)
        if self.in_use:
            Registries.changed()

    def get_mappings(self, field_class: type) -> tuple[TypeMapping, ...]:
        """The mappings registered for ``field_class`` and its bases, nearest first.

        Args:
            field_class: The field class.

        Returns:
            The mappings.
        """
        mappings = self.mappings_by_field_class.get(field_class)
        if mappings is None:
            mappings = tuple(self.mappings[base] for base in field_class.__mro__ if base in self.mappings)
            self.mappings_by_field_class[field_class] = mappings
        return mappings

    def get_column_type(self, field: Field[Any]) -> str | None:
        """The column type the dialect gives ``field``, or None when it keeps the field's own."""
        for mapping in self.get_mappings(type(field)):
            if mapping.column_type is not None:
                column_type = mapping.column_type
                return column_type if isinstance(column_type, str) else column_type(field)
        return None

    def get_generated_sql(self, field: Field[Any]) -> str | None:
        """The DDL the dialect gives a generated primary key column of ``field``."""
        for mapping in self.get_mappings(type(field)):
            if mapping.generated_sql is not None:
                return mapping.generated_sql
        return None

    def get_function_cast(self, field: Field[Any]) -> Callable[[Field[Any], Term], Term] | None:
        """The cast the dialect wraps a column of ``field`` in, if any."""
        for mapping in self.get_mappings(type(field)):
            if mapping.function_cast is not None:
                return mapping.function_cast
        return None

    def get_json_term(self, field: Field[Any], term: Term) -> Term | None:
        """The term a column of ``field`` is written into a JSON object as.

        Args:
            field: The column's field.
            term: The column.

        Returns:
            The dialect's ``json_term`` of it, None when the value goes in as it is.
        """
        for mapping in self.get_mappings(type(field)):
            if mapping.json_term is not None:
                return mapping.json_term(field, term)
        return None

    def get_db_converter(self, field_class: type) -> DbConverter | None:
        """The ``to_db`` the dialect registers for ``field_class``, or None when the field's own
        ``to_db_value()`` applies."""
        try:
            return self.db_converters[field_class]
        except KeyError:
            converter = self.db_converters[field_class] = next(
                (mapping.to_db for mapping in self.get_mappings(field_class) if mapping.to_db is not None), None
            )
            return converter

    def get_lookup_converter(self, field_class: type) -> DbConverter | None:
        """The ``to_lookup`` the dialect registers for ``field_class``, or None when the field's own
        ``to_lookup_value()`` applies."""
        try:
            return self.lookup_converters[field_class]
        except KeyError:
            converter = self.lookup_converters[field_class] = next(
                (mapping.to_lookup for mapping in self.get_mappings(field_class) if mapping.to_lookup is not None),
                None,
            )
            return converter

    def get_python_converter(self, field_class: type) -> PythonConverter | None:
        """The ``to_python`` the dialect registers for ``field_class``, or None when the field's own
        ``from_db_value()`` applies."""
        try:
            return self.python_converters[field_class]
        except KeyError:
            converter = self.python_converters[field_class] = next(
                (mapping.to_python for mapping in self.get_mappings(field_class) if mapping.to_python is not None),
                None,
            )
            return converter

    def reads_naive_datetime_as_utc(self, field_class: type) -> bool:
        """Whether the ``to_python`` the dialect registers for ``field_class`` is the field's own
        reading, with a naive datetime from the driver taken as a UTC instant."""
        return next(
            (mapping.naive_datetime_is_utc for mapping in self.get_mappings(field_class) if mapping.to_python),
            False,
        )

    def get_db_value(self, field: Field[Any], value: Any, instance: type[Model] | Model | None) -> Any:
        """The value bound for a column of ``field``.

        Args:
            field: The field.
            value: The Python value.
            instance: The model (class) the value is written or compared for.

        Returns:
            The value to bind.
        """
        converter = self.get_db_converter(type(field))
        if converter is None:
            return field.to_db_value(value, instance)  # type: ignore[arg-type]
        return converter(field, value, instance)

    def get_lookup_value(self, field: Field[Any], value: Any, instance: type[Model] | Model | None) -> Any:
        """The value bound for a filter on a column of ``field``.

        Args:
            field: The field.
            value: The Python value.
            instance: The model (class) the filter is built for.

        Returns:
            The value to bind.
        """
        converter = self.get_lookup_converter(type(field))
        if converter is None:
            return field.to_lookup_value(value, instance)  # type: ignore[arg-type]
        return converter(field, value, instance)

    def get_python_value(self, field: Field[Any], value: Any) -> Any:
        """A value read from a column of ``field``, as the field holds it.

        Args:
            field: The field.
            value: The value the driver returned.

        Returns:
            The Python value.
        """
        converter = self.get_python_converter(type(field))
        if converter is None:
            return field.from_db_value(value)
        return converter(field, value)

    def get_db_writer(self, field: Field[Any]) -> Callable[[Any, Any], Any]:
        """A ``(value, instance) -> value`` converting values bound for a column of ``field``.

        Args:
            field: The field.

        Returns:
            The field's own ``to_db_value`` when the dialect registers no ``to_db`` for it.
        """
        converter = self.get_db_converter(type(field))
        if converter is None:
            return field.to_db_value
        return functools.partial(converter, field)

    def get_python_reader(self, field: Field[Any]) -> Callable[[Any], Any]:
        """A ``(value) -> value`` converting values read from a column of ``field``.

        Args:
            field: The field.

        Returns:
            The field's own ``from_db_value`` when the dialect registers no ``to_python`` for it.
        """
        converter = self.get_python_converter(type(field))
        if converter is None:
            return field.from_db_value
        return functools.partial(converter, field)

    def get_generated_db_value(self, field: GeneratedField, value: Any, instance: type[Model] | Model | None) -> Any:
        """Converts a value of a generated column as a value of its output field."""
        return self.get_db_value(field.output_field, value, instance)

    def get_generated_lookup_value(
        self, field: GeneratedField, value: Any, instance: type[Model] | Model | None
    ) -> Any:
        """Converts a filter value on a generated column as a value of its output field."""
        return self.get_db_value(field.output_field, value, instance)

    def get_generated_python_value(self, field: GeneratedField, value: Any) -> Any:
        """Converts a value read from a generated column as a value of its output field."""
        return self.get_python_value(field.output_field, value)
