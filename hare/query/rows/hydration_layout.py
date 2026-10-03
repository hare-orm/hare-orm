from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.core.cache import Cache
from hare.fields.base.field import Field
from hare.models.enums import FieldBucket

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.types.type_registry import TypeRegistry
    from hare.models.meta_info import MetaInfo

if TYPE_CHECKING:  # pragma: nocoverage
    HydrationEntry = tuple[str, Field[Any], FieldBucket, Callable[[Any], Any] | None]


@dataclass(frozen=True, slots=True)
class HydrationLayout:
    """How the rows of one model are read on one type of connection: each column's hydration
    bucket and reader, which depend on the driver's native types and the dialect's type hooks.

    Attributes:
        native_fields: ``(column, model_field_name, field)`` of columns used as the driver returns them.
        default_fields: ``(column, model_field_name, field)`` of columns converted by the field type.
        complex_fields: ``(column, model_field_name, reader)`` of columns converted by a reader.
        entry_by_column: Column name -> its entry.
        entry_by_field_name: Model field name -> ``(column, field, bucket, dialect_reader)``.
        reader_by_column: Column name -> the call converting its raw value, None for a column used
            as the driver returns it.
        reader_by_field_name: Model field name -> the same reader as in ``reader_by_column``.
        full_column_count: How many columns a full row has.
    """

    #: A model's layouts, in a bucket the model keeps: (driver's native Python types, dialect) ->
    #: how the model's rows are read on such a connection.
    layouts: ClassVar[Cache[Any]] = Cache(holds_sql=False, keyed_by_model=False, model_attribute="hydration_layouts")

    native_fields: tuple[tuple[str, str, Field[Any]], ...]
    default_fields: tuple[tuple[str, str, Field[Any]], ...]
    complex_fields: tuple[tuple[str, str, Callable[[Any], Any]], ...]
    entry_by_column: dict[str, HydrationEntry]
    entry_by_field_name: dict[str, tuple[str, Field[Any], FieldBucket, Callable[[Any], Any] | None]]
    reader_by_column: dict[str, Callable[[Any], Any] | None]
    reader_by_field_name: dict[str, Callable[[Any], Any] | None]
    full_column_count: int

    @staticmethod
    def build(meta: MetaInfo, db: DatabaseClient) -> HydrationLayout:
        """The layout of ``meta``'s model on ``db``.

        Args:
            meta: The model's meta.
            db: The connection the rows are read on.

        Returns:
            The layout.
        """
        types = db.dialect.types
        native_types = db.native_python_types
        native_fields: list[tuple[str, str, Field[Any]]] = []
        default_fields: list[tuple[str, str, Field[Any]]] = []
        complex_fields: list[tuple[str, str, Callable[[Any], Any]]] = []
        entry_by_column: dict[str, HydrationEntry] = {}
        entry_by_field_name: dict[str, tuple[str, Field[Any], FieldBucket, Callable[[Any], Any] | None]] = {}
        reader_by_column: dict[str, Callable[[Any], Any] | None] = {}
        reader_by_field_name: dict[str, Callable[[Any], Any] | None] = {}
        for column in meta.db_fields:
            model_field = meta.fields_db_projection_reverse[column]
            field = meta.fields_map[model_field]
            bucket, reader, dialect_reader = HydrationLayout.get_field_reading(field, types, native_types)
            if bucket == FieldBucket.NATIVE:
                native_fields.append((column, model_field, field))
            elif bucket == FieldBucket.DEFAULT:
                default_fields.append((column, model_field, field))
            else:
                complex_fields.append((column, model_field, cast("Callable[[Any], Any]", reader)))
            entry_by_column[column] = (model_field, field, bucket, dialect_reader)
            entry_by_field_name[model_field] = (column, field, bucket, dialect_reader)
            reader_by_column[column] = reader_by_field_name[model_field] = reader
        return HydrationLayout(
            native_fields=tuple(native_fields),
            default_fields=tuple(default_fields),
            complex_fields=tuple(complex_fields),
            entry_by_column=entry_by_column,
            entry_by_field_name=entry_by_field_name,
            reader_by_column=reader_by_column,
            reader_by_field_name=reader_by_field_name,
            full_column_count=len(entry_by_column),
        )

    @staticmethod
    def get_field_reading(
        field: Field[Any], types: TypeRegistry, native_types: frozenset[type]
    ) -> tuple[FieldBucket, Callable[[Any], Any] | None, Callable[[Any], Any] | None]:
        """How a column of ``field`` is read on a connection.

        Args:
            field: The field.
            types: The dialect's type registry.
            native_types: The Python types the driver returns values as already.

        Returns:
            The bucket, the reader (None for a value used as the driver returns it) and the
            dialect's reader (None when the dialect registers none).
        """
        dialect_reader = types.get_python_reader(field) if types.get_python_converter(type(field)) else None
        if dialect_reader is not None:
            return FieldBucket.COMPLEX, dialect_reader, dialect_reader
        if field.field_type in native_types and field.keeps_native_db_values:
            return FieldBucket.NATIVE, None, None
        if type(field).from_db_value is Field.from_db_value and type(field).to_python is Field.to_python:
            return FieldBucket.DEFAULT, HydrationLayout.get_field_type_reader(field.field_type), None
        return FieldBucket.COMPLEX, field.from_db_value, None

    @staticmethod
    def get_field_type_reader(field_type: Callable[[Any], Any]) -> Callable[[Any], Any]:
        """The reader of a column its field converts by calling its own Python type - a NULL
        stays None.

        Args:
            field_type: The field's Python type.

        Returns:
            The reader.
        """

        def read(value: Any) -> Any:
            return value if value is None else field_type(value)

        return read
