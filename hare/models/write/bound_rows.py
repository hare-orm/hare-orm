from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from hare.models.write.returned_values import ReturnedValues

if TYPE_CHECKING:
    from hare.dialects.base.types.type_registry import TypeRegistry
    from hare.models.model_binding import ModelBinding


class BoundRows:
    """Converted input rows shared by SQL binding and returned-row matching.

    Values belong to one execution. Matching reads these database values through
    the selected registry; it never writes the original Python inputs again.
    Subsets keep the same column layout and original converted cell objects.
    """

    def __init__(self, field_names: Sequence[str], values: Sequence[Sequence[Any]]) -> None:
        self.field_names = tuple(field_names)
        self.values = values

    def get_keys(
        self, binding: ModelBinding, types: TypeRegistry, field_names: Sequence[str]
    ) -> list[tuple[Any, ...]]:
        """Read hashable comparison keys from columns already bound for this write."""
        readers = tuple(
            (self.field_names.index(name), types.get_python_reader(binding.fields_map[name])) for name in field_names
        )
        return [
            tuple(ReturnedValues.get_hashable(read(row[position])) for position, read in readers)
            for row in self.values
        ]

    def select(self, indices: Sequence[int]) -> BoundRows:
        """Retain converted rows in the same order as a selected object subset."""
        return BoundRows(self.field_names, [self.values[index] for index in indices])
