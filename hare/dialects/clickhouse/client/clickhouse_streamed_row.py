from __future__ import annotations

from typing import Any, ClassVar, Self

from hare.core.caching.cache import Cache
from hare.query.rows.constants import ROW_CLASS_CACHE_SIZE


class ClickhouseStreamedRow(tuple[Any, ...]):
    """A row of a streamed query - its values by position, as the readers of a batch take them, and by
    column name, as a mapping. A subclass per set of columns holds the positions of their names."""

    __slots__ = ()

    #: The position of each column by its name - of the subclass of a set of columns.
    column_positions: ClassVar[dict[str, int]] = {}
    #: The subclass of each set of columns met, filled as they are met.
    row_classes: ClassVar[Cache[type[ClickhouseStreamedRow]]] = Cache(
        ROW_CLASS_CACHE_SIZE, holds_sql=False, keyed_by_model=False
    )

    @classmethod
    def get_row_class(cls, column_names: tuple[str, ...]) -> type[Self]:
        """The class of the rows of a set of columns.

        Args:
            column_names: The columns, in order.

        Returns:
            The class.
        """
        key = (cls, column_names)
        row_class = ClickhouseStreamedRow.row_classes.get(key)
        if row_class is None:
            row_class = ClickhouseStreamedRow.row_classes[key] = type(
                cls.__name__,
                (cls,),
                {
                    "__slots__": (),
                    "column_positions": {name: position for position, name in enumerate(column_names)},
                },
            )
        return row_class

    def keys(self) -> Any:
        """The names of the row's columns."""
        return self.column_positions.keys()

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, str):
            return tuple.__getitem__(self, self.column_positions[key])
        return tuple.__getitem__(self, key)
