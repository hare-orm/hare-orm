from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from hare.native.native_modules import NativeModules
from hare.query.rows.native.hydration_layout import HydrationLayout

if TYPE_CHECKING:
    from hare.models.model_binding import ModelBinding
    from hare.query.rows.native.hydration_layout import HydrationEntry


class ModelColumns:
    """Build native layouts for one model's columns in the result."""

    @staticmethod
    def from_entries(
        binding: ModelBinding,
        entries: Sequence[HydrationEntry],
        keys: Sequence[int | str],
        is_partial: bool,
        path: tuple[str, ...] = (),
        is_related: bool = False,
    ) -> Any:
        """Prepare already matched columns without storing the original fields and row keys."""
        columns = tuple((name, key, reader) for (name, _field, reader), key in zip(entries, keys, strict=True))
        return NativeModules.rows.ModelRowLayout(binding, columns, path, is_partial, is_related)

    @staticmethod
    def build(
        binding: ModelBinding,
        layout: HydrationLayout,
        names: Sequence[str],
        row_keys: Sequence[int | str],
        *,
        path: tuple[str, ...] = (),
        is_related: bool = False,
        names_are_fields: bool = False,
    ) -> Any:
        """The native layout for one model's columns in a result.

        Args:
            binding: The model's binding.
            layout: How the model's rows are read on the connection.
            names: What each of the model's columns in the result is named after - a column name
                of the model, or a field name of it (a joined relation's columns).
            row_keys: Where each is read from a row.
            path: The relation path to the instance.
            is_related: Whether the columns are those of a joined relation.
            names_are_fields: Names are logical model attributes, including source_field aliases;
                do not interpret a matching physical column name as another attribute.

        Returns:
            The native layout. Names that identify neither a column nor a model field are left out.
        """
        columns = []
        for name, row_key in zip(names, row_keys, strict=True):
            entry = None if names_are_fields else layout.entry_by_column.get(name)
            if entry is not None:
                field_name, _field, reader = entry
            else:
                field_entry = layout.entry_by_field_name.get(name)
                if field_entry is None:
                    continue
                field_name = name
                _, _field, reader = field_entry
            columns.append((field_name, row_key, reader))
        return NativeModules.rows.ModelRowLayout(
            binding, tuple(columns), path, len(columns) < layout.full_column_count, is_related
        )
