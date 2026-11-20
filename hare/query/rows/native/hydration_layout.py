from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from hare.core.caching.cache import Cache

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.fields.field import Field
    from hare.models.model_binding import ModelBinding

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
    layouts: ClassVar[Cache[Any]] = Cache()

    entry_by_column: dict[str, HydrationEntry]
    entry_by_field_name: dict[str, tuple[str, Field[Any], Callable[[Any], Any] | None]]
    full_column_count: int

    @staticmethod
    def build(meta: ModelBinding, connection: DatabaseClient) -> HydrationLayout:
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
            reader = types.get_native_reader(field, native_types)
            entry_by_column[column] = (model_field, field, reader)
            entry_by_field_name[model_field] = (column, field, reader)
        return HydrationLayout(
            entry_by_column=entry_by_column,
            entry_by_field_name=entry_by_field_name,
            full_column_count=len(entry_by_column),
        )
