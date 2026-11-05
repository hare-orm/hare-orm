from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from hare.core.caching.cache import Cache
from hare.fields.field import Field

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.types.type_registry import TypeRegistry
    from hare.models.meta_info import MetaInfo

    HydrationEntry = tuple[str, Field[Any], Callable[[Any], Any] | None]


@dataclass(frozen=True, slots=True)
class HydrationLayout:
    """The field and bound reader of each model column on one type of connection.

    Attributes:
        entry_by_column: Column name -> (model field name, field, reader).
        entry_by_field_name: Model field name -> (column name, field, reader).
        full_column_count: How many columns a full row has.

    A None reader preserves the driver's value. Every other entry carries the complete
    conversion chosen from the dialect hook, native types and field's Python conversion.
    """

    #: A model's layouts, in a bucket the model keeps: (driver's native Python types, dialect) ->
    #: how the model's rows are read on such a connection.
    layouts: ClassVar[Cache[Any]] = Cache(holds_sql=False)

    entry_by_column: dict[str, HydrationEntry]
    entry_by_field_name: dict[str, tuple[str, Field[Any], Callable[[Any], Any] | None]]
    full_column_count: int

    @staticmethod
    def build(meta: MetaInfo, connection: DatabaseClient) -> HydrationLayout:
        """The layout of ``meta``'s model on ``connection``.

        Args:
            meta: The model's meta.
            connection: The connection the rows are read on.

        Returns:
            The layout.
        """
        types = connection.dialect.types
        native_types = connection.native_python_types
        entry_by_column: dict[str, HydrationEntry] = {}
        entry_by_field_name: dict[str, tuple[str, Field[Any], Callable[[Any], Any] | None]] = {}
        for column in meta.db_fields:
            model_field = meta.fields_db_projection_reverse[column]
            field = meta.fields_map[model_field]
            reader = HydrationLayout.get_field_reader(field, types, native_types)
            entry_by_column[column] = (model_field, field, reader)
            entry_by_field_name[model_field] = (column, field, reader)
        return HydrationLayout(
            entry_by_column=entry_by_column,
            entry_by_field_name=entry_by_field_name,
            full_column_count=len(entry_by_column),
        )

    @staticmethod
    def get_field_reader(
        field: Field[Any], types: TypeRegistry, native_types: frozenset[type]
    ) -> Callable[[Any], Any] | None:
        """How a column of ``field`` is read on a connection.

        Args:
            field: The field.
            types: The dialect's type registry.
            native_types: The Python types the driver returns values as already.

        Returns:
            The complete reader, or None for a value used as the driver returns it.
        """
        dialect_reader = types.get_python_reader(field) if types.get_python_converter(type(field)) else None
        if dialect_reader is not None:
            return dialect_reader
        if field.field_type in native_types and field.keeps_native_db_values:
            return None
        if type(field).from_db_value is Field.from_db_value and type(field).to_python is Field.to_python:
            return HydrationLayout.get_field_type_reader(field.field_type)
        return field.from_db_value

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
