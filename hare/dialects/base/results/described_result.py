from __future__ import annotations

import dataclasses
from typing import Any


@dataclasses.dataclass(frozen=True, slots=True)
class DescribedResult:
    """What one SQL statement returned, with its columns - ``DatabaseClient.execute_described()``.

    Attributes:
        columns: The names of the result's columns, in order - for an empty result too; none for
            a statement that returns no rows (an ``UPDATE`` without ``RETURNING``, DDL).
        rows: The rows, each a tuple of values in the order of ``columns`` - two columns of one
            name both keep their value.
        row_count: The rows the statement changed, for a write without ``RETURNING``; else the
            rows it returned.
    """

    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    row_count: int
