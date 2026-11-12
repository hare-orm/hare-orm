from __future__ import annotations

import functools
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ClassVar

from hare.classes.deferred_import import DeferredImport
from hare.core.caching.cache import Cache
from hare.core.caching.cache_bucket import CacheBucket
from hare.core.extensions.snapshot_mapping import SnapshotMapping
from hare.dialects.base.types.constants import NATIVE_KEY_VALUE_TYPES
from hare.dialects.base.types.prepared_converter import PreparedConverter
from hare.dialects.base.types.type_mapping import TypeMapping

if TYPE_CHECKING:
    from hare.dialects.base.types.type_mapping import DbConverter, PythonConverter
    from hare.fields.field import Field
    from hare.fields.generated_field import GeneratedField
    from hare.models import Model
    from hare.sql.terms.term import Term


class TypeRegistry:
    """The type mappings of one dialect, found through a field's class hierarchy - a field class
    of its own uses the mapping of the nearest registered base class."""

    #: ``GeneratedField`` - imported at its first read, as its module imports this one.
    generated_field_class: ClassVar[DeferredImport[type[GeneratedField]]] = DeferredImport(
        "hare.fields.generated_field", "GeneratedField"
    )

    #: What each registry found for a field class through the class's bases, kept in a bucket of
    #: the registry: its mappings, nearest first, and its ``to_db``, ``to_lookup`` and ``to_python``.
    found_mappings: ClassVar[Cache[Any]] = Cache(holds_sql=False)
    #: The mappings each registry gives a field class its column type from, nearest first.
    found_column_type_mappings: ClassVar[Cache[Any]] = Cache(holds_sql=False)
    found_db_converters: ClassVar[Cache[Any]] = Cache(holds_sql=False)
    found_lookup_converters: ClassVar[Cache[Any]] = Cache(holds_sql=False)
    found_python_converters: ClassVar[Cache[Any]] = Cache(holds_sql=False)
    #: The native key type each registry found for a field class and its value type.
    found_native_key_types: ClassVar[Cache[Any]] = Cache(holds_sql=False)

    def __init__(self) -> None:
        from hare.fields.generated_field import GeneratedField

        #: The buckets of the caches this registry reads (``Cache.get_bucket()``).
        self.cache_buckets: dict[int, Any] = {}
        self.mappings: SnapshotMapping[type, TypeMapping] = SnapshotMapping()
        #: The registries of the dialect's variants (``Dialect.variants``) - a registration is repeated there.
        self.mirrors: list[TypeRegistry] = []
        self.mappings_by_field_class: CacheBucket[tuple[TypeMapping, ...]] = TypeRegistry.found_mappings.get_bucket(
            self
        )
        self.column_type_mappings_by_field_class: CacheBucket[tuple[TypeMapping, ...]] = (
            TypeRegistry.found_column_type_mappings.get_bucket(self)
        )
        self.db_converters: CacheBucket[DbConverter | None] = TypeRegistry.found_db_converters.get_bucket(self)
        self.lookup_converters: CacheBucket[DbConverter | None] = TypeRegistry.found_lookup_converters.get_bucket(self)
        self.python_converters: CacheBucket[PythonConverter | None] = TypeRegistry.found_python_converters.get_bucket(
            self
        )
        self.native_key_types: CacheBucket[type | None] = TypeRegistry.found_native_key_types.get_bucket(self)
        #: Whether a value held in a container (an array's element) may be NULL whatever its field
        #: declares - else only a field with ``null=True`` holds one there.
        self.container_values_always_nullable = False
        self.register(
            GeneratedField,
            TypeMapping(
                to_db=PreparedConverter(self.get_generated_db_converter),
                to_lookup=PreparedConverter(self.get_generated_lookup_converter),
                to_python=PreparedConverter(self.get_generated_python_converter),
            ),
        )

    def register(self, field_class: type, mapping: TypeMapping) -> None:
        """Sets how the dialect stores ``field_class`` and its subclasses.

        Args:
            field_class: The field class.
            mapping: Its mapping.
        """
        self.mappings.register(field_class, mapping, tuple(mirror.mappings for mirror in self.mirrors))
        if not self.mappings.sealed:
            Cache.clear_holder(self)

    @property
    def has_inserted_by_select(self) -> bool:
        """Whether this execution's mappings require SELECT-based row insertion."""
        return any(mapping.inserted_by_select is not False for mapping in self.mappings.values())

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

    def get_column_type_mappings(self, field_class: type[Field[Any]]) -> tuple[TypeMapping, ...]:
        """The mappings a field class takes its column type from, nearest first - for a class taking it
        from the dialect (``COLUMN_TYPE_FROM_DIALECT``) only those of its class and the classes between it
        and the one setting that: a field of one database never takes the column type another database
        gives a base class of it.

        Args:
            field_class: The field class.

        Returns:
            The mappings.
        """
        mappings = self.column_type_mappings_by_field_class.get(field_class)
        if mappings is None:
            bases: tuple[type, ...] = field_class.__mro__
            if field_class.COLUMN_TYPE_FROM_DIALECT:
                declaring_class = next(base for base in bases if "COLUMN_TYPE_FROM_DIALECT" in vars(base))
                bases = tuple(base for base in bases if issubclass(base, declaring_class))
            mappings = tuple(self.mappings[base] for base in bases if base in self.mappings)
            self.column_type_mappings_by_field_class[field_class] = mappings
        return mappings

    def get_column_type(self, field: Field[Any]) -> str | None:
        """The column type the dialect gives ``field``, or None when it keeps the field's own."""
        for mapping in self.get_column_type_mappings(type(field)):
            if mapping.column_type is not None:
                column_type = mapping.column_type
                return column_type if isinstance(column_type, str) else column_type(field)
        return None

    def is_inserted_by_select(self, field: Field[Any]) -> bool:
        """Whether an INSERT writing ``field``'s column writes its rows as a ``SELECT`` of them.

        Args:
            field: The field.

        Returns:
            The nearest mapping's ``inserted_by_select`` - False by default.
        """
        for mapping in self.get_mappings(type(field)):
            inserted_by_select = mapping.inserted_by_select
            if inserted_by_select is not False:
                return inserted_by_select if isinstance(inserted_by_select, bool) else inserted_by_select(field)
        return False

    def get_extension(self, field: Field[Any]) -> str | None:
        """The database extension the dialect's column type of ``field`` needs, if any."""
        for mapping in self.get_mappings(type(field)):
            if mapping.extension is not None:
                return mapping.extension
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

    def get_ordering_key(self, field: Field[Any], term: Term) -> Term:
        """What a column of ``field`` is ordered by.

        Args:
            field: The column's field.
            term: The column.

        Returns:
            The dialect's ``ordering_key`` of it, else the column itself.
        """
        field = self.get_value_field(field)
        for mapping in self.get_mappings(type(field)):
            if mapping.ordering_key is not None:
                ordering_key = mapping.ordering_key(field, term)
                return term if ordering_key is None else ordering_key
        return term

    def has_ordering_key(self, field: Field[Any]) -> bool:
        """Whether the dialect may order a column of ``field`` by a key of it (``get_ordering_key()``).

        Args:
            field: The column's field.

        Returns:
            Whether it does.
        """
        field = self.get_value_field(field)
        return any(mapping.ordering_key is not None for mapping in self.get_mappings(type(field)))

    def get_value_field(self, field: Field[Any]) -> Field[Any]:
        """The field whose mappings a value of ``field`` follows - a ``GeneratedField``'s output field.

        Args:
            field: The field.

        Returns:
            The output field of a generated field, else the field itself.
        """
        if field.generated and isinstance(field, TypeRegistry.generated_field_class):
            return field.output_field
        return field

    def holds_null(self, field: Field[Any]) -> bool:
        """Whether the dialect's column type of ``field`` holds NULL.

        Args:
            field: The field.

        Returns:
            The nearest mapping's ``holds_null`` - True by default.
        """
        field = self.get_value_field(field)
        for mapping in self.get_mappings(type(field)):
            if mapping.holds_null is not None:
                return mapping.holds_null
        return True

    def get_json_term(self, field: Field[Any], term: Term) -> Term | None:
        """The term a column of ``field`` is written into a JSON object as.

        Args:
            field: The column's field.
            term: The column.

        Returns:
            The dialect's ``json_term`` of it, None when the value goes in as it is.
        """
        field = self.get_value_field(field)
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
            converter = self.db_converters[field_class] = self.find_converter(field_class, "to_db")
            return converter

    def get_native_key_type(self, field: Field[Any]) -> type | None:
        """The type of values unchanged by both writing and reading the field. A value of exactly that
        type is its own stored and decoded key: the field writes it unchanged (possibly with native
        checks), keeps native read values, and the dialect registers no conversion changing either form.

        Args:
            field: The field.

        Returns:
            The type, None for a field whose values are converted.
        """
        if (
            getattr(self.get_db_value, "__func__", None) is not TypeRegistry.get_db_value
            or getattr(self.get_python_value, "__func__", None) is not TypeRegistry.get_python_value
            or getattr(self.get_db_converter, "__func__", None) is not TypeRegistry.get_db_converter
            or getattr(self.get_python_converter, "__func__", None) is not TypeRegistry.get_python_converter
        ):
            return None
        field_class = type(field)
        key = (field_class, field.field_type)
        try:
            return self.native_key_types[key]
        except KeyError:
            pass
        # Local import: fields import the dialects' registries during their setup.
        from hare.fields.field import Field

        native_type = (
            field.field_type
            if field.field_type in NATIVE_KEY_VALUE_TYPES
            and field.keeps_native_db_values
            and field_class.to_db_value is Field.to_db_value
            and self.get_db_converter(field_class) is None
            and self.get_python_converter(field_class) is None
            else None
        )
        self.native_key_types[key] = native_type
        return native_type

    def get_lookup_converter(self, field_class: type) -> DbConverter | None:
        """The ``to_lookup`` the dialect registers for ``field_class``, or None when the field's own
        ``to_lookup_value()`` applies."""
        try:
            return self.lookup_converters[field_class]
        except KeyError:
            converter = self.lookup_converters[field_class] = self.find_converter(field_class, "to_lookup")
            return converter

    def get_python_converter(self, field_class: type) -> PythonConverter | None:
        """The ``to_python`` the dialect registers for ``field_class``, or None when the field's own
        ``from_db_value()`` applies."""
        try:
            return self.python_converters[field_class]
        except KeyError:
            converter = self.python_converters[field_class] = self.find_converter(field_class, "to_python")
            return converter

    def find_converter(self, field_class: type, converter_name: str) -> Any:
        """The first converter of a name the mappings of ``field_class`` register, through its bases.

        Args:
            field_class: The field class.
            converter_name: ``to_db``, ``to_lookup`` or ``to_python``.

        Returns:
            The converter, None when no mapping registers one.
        """
        for mapping in self.get_mappings(field_class):
            converter = getattr(mapping, converter_name)
            if converter is not None:
                return converter
        return None

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
            return field.to_db_value(value, instance)
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
            return field.to_lookup_value(value, instance)
        return converter(field, value, instance)

    def get_read_column_value(self, field: Field[Any], value: Any, instance: type[Model] | Model | None) -> Any:
        """A value of ``field`` as the column reads where it is compared - a value that stands with the
        column's own (a window's default, a cursor's boundary): a filter's value (a float32 as a float32,
        an enum by its label), a JSON value as the column stores it.

        Args:
            field: The field.
            value: The Python value, not None.
            instance: The model (class) the query is built for.

        Returns:
            The value to bind.
        """
        return self.get_read_column_writer(field)(value, instance)

    def get_lookup_values(
        self, field: Field[Any], values: list[Any], instance: type[Model] | Model | None
    ) -> list[Any]:
        """``get_lookup_value()`` of each of ``values`` - a column of key values compared at once, the
        field checking them in one pass.

        Args:
            field: The field.
            values: The Python values; None preserves its position without conversion.
            instance: The model (class) the filter is built for.

        Returns:
            The values to bind, in order.
        """
        return self.get_lookup_values_writer(field)(values, instance)

    def get_lookup_values_writer(self, field: Field[Any]) -> Callable[[list[Any], Any], list[Any]]:
        """Prepare a column conversion, retaining batch hooks and the positions of NULLs.

        Custom value or selector methods remain live for every non-NULL element.
        Standard fields may validate a homogeneous column through their batch hook.
        """
        write = self.get_lookup_writer(field)
        batch = (
            field.to_lookup_values
            if getattr(self.get_lookup_value, "__func__", None) is TypeRegistry.get_lookup_value
            and getattr(self.get_lookup_converter, "__func__", None) is TypeRegistry.get_lookup_converter
            and self.get_lookup_converter(type(field)) is None
            else None
        )

        def write_values(values: list[Any], instance: Any) -> list[Any]:
            if batch is not None and not any(value is None for value in values):
                return batch(values, instance)
            return [None if value is None else write(value, instance) for value in values]

        return write_values

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
            The field's writer (``Field.get_db_value_writer()``) of the dialect's ``to_db`` for it, or of its
            own ``to_db_value`` when the dialect registers none.
        """
        if (
            getattr(self.get_db_value, "__func__", None) is not TypeRegistry.get_db_value
            or getattr(self.get_db_converter, "__func__", None) is not TypeRegistry.get_db_converter
        ):
            return functools.partial(self.get_db_value, field)
        return field.get_db_value_writer(self.get_db_converter(type(field)))

    def get_lookup_writer(self, field: Field[Any]) -> Callable[[Any, Any], Any]:
        """A ``(value, instance) -> value`` converting values a filter on a column of ``field`` binds.

        Args:
            field: The field.

        Returns:
            The field's own converter (``Field.get_lookup_value_writer()``) when the dialect registers no
            ``to_lookup`` for it.
        """
        if (
            getattr(self.get_lookup_value, "__func__", None) is not TypeRegistry.get_lookup_value
            or getattr(self.get_lookup_converter, "__func__", None) is not TypeRegistry.get_lookup_converter
        ):
            return functools.partial(self.get_lookup_value, field)
        converter = self.get_lookup_converter(type(field))
        if converter is None:
            return field.get_lookup_value_writer()
        return (
            converter.prepare(field)
            if isinstance(converter, PreparedConverter)
            else functools.partial(converter, field)
        )

    def get_python_reader(self, field: Field[Any]) -> Callable[[Any], Any]:
        """A ``(value) -> value`` converting values read from a column of ``field``.

        Args:
            field: The field.

        Returns:
            The field's own reader (``Field.get_db_value_reader()``) when the dialect registers no
            ``to_python`` for it.
        """
        if (
            getattr(self.get_python_value, "__func__", None) is not TypeRegistry.get_python_value
            or getattr(self.get_python_converter, "__func__", None) is not TypeRegistry.get_python_converter
        ):
            return functools.partial(self.get_python_value, field)
        converter = self.get_python_converter(type(field))
        if converter is None:
            return field.get_db_value_reader()
        return (
            converter.prepare(field)
            if isinstance(converter, PreparedConverter)
            else functools.partial(converter, field)
        )

    def get_field_encoder(self, field: Field[Any], instance: type[Model] | None = None) -> Callable[[Any], Any]:
        """Prepare a literal's storage conversion with an optional model argument.

        The regular write conversion retains dialect converters and field overrides.
        """
        return TypeRegistry.get_encoder(self.get_db_writer(field), instance)

    def get_native_reader(self, field: Field[Any], native_types: frozenset[type]) -> Callable[[Any], Any] | None:
        """Prepare hydration, skipping conversion only when the registry's value contract permits it."""
        if (
            getattr(self.get_python_value, "__func__", None) is not TypeRegistry.get_python_value
            or getattr(self.get_python_converter, "__func__", None) is not TypeRegistry.get_python_converter
        ):
            return TypeRegistry.get_python_reader(self, field)
        if self.get_python_converter(type(field)) is not None:
            return self.get_python_reader(field)
        if field.field_type in native_types and field.keeps_native_db_values:
            return None
        return field.get_db_value_reader()

    def get_lookup_encoder(self, field: Field[Any], instance: type[Model] | None) -> Callable[[Any], Any]:
        """Bind the model argument of a prepared lookup conversion."""
        return TypeRegistry.get_encoder(self.get_lookup_writer(field), instance)

    def get_read_column_writer(self, field: Field[Any]) -> Callable[[Any, Any], Any]:
        """Prepare the conversion shared by cursor boundaries and window defaults."""
        from hare.fields.data.json.json_field import JSONField

        if isinstance(self.get_value_field(field), JSONField):
            return self.get_db_writer(field)
        return self.get_lookup_writer(field)

    def get_read_column_encoder(self, field: Field[Any], instance: type[Model] | None) -> Callable[[Any], Any]:
        """Bind a prepared column comparison without retaining an execution context."""
        return TypeRegistry.get_encoder(self.get_read_column_writer(field), instance)

    @staticmethod
    def get_encoder(writer: Callable[[Any, Any], Any], instance: type[Model] | None) -> Callable[[Any], Any]:
        """Adapt a column writer to the single-value expression encoder contract."""
        return lambda value: writer(value, instance)

    def get_generated_db_converter(self, field: Field[Any]) -> Callable[[Any, Any], Any]:
        """Prepare storage conversion through the generated column's output field."""
        return TypeRegistry.get_db_writer(self, field.output_field)  # type: ignore[attr-defined]

    def get_generated_lookup_converter(self, field: Field[Any]) -> Callable[[Any, Any], Any]:
        """Prepare comparisons through the generated column's output field."""
        return TypeRegistry.get_lookup_writer(self, field.output_field)  # type: ignore[attr-defined]

    def get_generated_python_converter(self, field: Field[Any]) -> Callable[[Any], Any]:
        """Prepare reading through the generated column's output field."""
        return TypeRegistry.get_python_reader(self, field.output_field)  # type: ignore[attr-defined]
