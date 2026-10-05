from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.rows.values_rows.value_field import ValueField
    from hare.query.rows.values_rows.values_rows import ValuesRows


@dataclasses.dataclass(frozen=True, slots=True)
class ValuesReading:
    """How the rows of a ``.values()``/``.values_list()`` query are read - kept with its plan, so a
    query running on the plan reads them without working it out again.

    Attributes:
        rows: The reader of the row shape asked for.
        column_converters: Each selected column's alias and the function decoding its value, None
            when the raw value is used as it is.
        value_fields: What each column is read as.
    """

    rows: ValuesRows
    column_converters: list[tuple[str, Callable[[Any], Any] | None]]
    value_fields: tuple[ValueField, ...]
