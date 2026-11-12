from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field as dataclass_field
from operator import itemgetter
from typing import TYPE_CHECKING, Any

from hare.native.native_modules import NativeModules
from hare.query.rows.native.hydration_layout import HydrationLayout
from hare.query.rows.row_column import RowColumn
from hare.query.rows.row_layout import RowLayout

if TYPE_CHECKING:
    from hare.models import Model
    from hare.query.rows.native.hydration_layout import HydrationEntry


@dataclass(slots=True)
class ModelColumns:
    """The columns of one model in the rows of a result: what each holds and where it sits.

    Attributes:
        model: The model.
        entries: The model field of each column.
        keys: Where each column is read from a row - its position, or its name on a driver
            whose rows aren't read by position.
        is_partial: Whether only some of the model's columns are selected.
        path: The relation path to the instance - empty for the queried model itself.
        is_related: Whether the columns are those of a joined relation - all NULL when the
            LEFT JOIN matched no row.
        native_layout: How the instances are made from the rows (``rust.native.rows.ModelRowLayout``) -
            made once with the columns, so a plan keeping the columns keeps it too.
    """

    model: type[Model]
    entries: tuple[HydrationEntry, ...]
    keys: tuple[int | str, ...]
    is_partial: bool
    path: tuple[str, ...] = ()
    is_related: bool = False
    row_layout: RowLayout = dataclass_field(init=False)
    field_names: tuple[str, ...] = dataclass_field(init=False)
    native_layout: Any = dataclass_field(init=False, compare=False, repr=False)

    def __post_init__(self) -> None:
        self.field_names = tuple(map(itemgetter(0), self.entries))
        self.row_layout = RowLayout(
            tuple(RowColumn(key, reader) for (_name, _field, reader), key in zip(self.entries, self.keys, strict=True))
        )
        self.native_layout = NativeModules.rows.ModelRowLayout(self)

    @staticmethod
    def build(
        model: type[Model],
        layout: HydrationLayout,
        names: Sequence[str],
        row_keys: Sequence[int | str],
        *,
        path: tuple[str, ...] = (),
        is_related: bool = False,
    ) -> ModelColumns:
        """The columns of ``model`` among a result's columns.

        Args:
            model: The model.
            layout: How the model's rows are read on the connection.
            names: What each of the model's columns in the result is named after - a column name
                of the model, or a field name of it (a joined relation's columns).
            row_keys: Where each is read from a row.
            path: The relation path to the instance.
            is_related: Whether the columns are those of a joined relation.

        Returns:
            The columns - a column that is neither a column nor a field of the model is left out.
        """
        entries: list[HydrationEntry] = []
        keys: list[int | str] = []
        for name, row_key in zip(names, row_keys, strict=True):
            entry = layout.entry_by_column.get(name)
            if entry is None:
                field_entry = layout.entry_by_field_name.get(name)
                if field_entry is None:
                    continue
                _, field, reader = field_entry
                entry = (name, field, reader)
            entries.append(entry)
            keys.append(row_key)
        return ModelColumns(
            model,
            tuple(entries),
            tuple(keys),
            len(entries) < layout.full_column_count,
            path,
            is_related,
        )
