from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hare.query.execution.write_statements import BoundWriteValue
    from hare.sql.terms.field import Field as SqlField


@dataclass(frozen=True, slots=True)
class ThroughValues:
    """Explicit through assignments converted once for one relation operation.

    Logical values remain available to write rules and plan descriptions. Insert and
    restore consume the same bound columns; neither rereads single-pass inputs.
    Per-row defaults and automatic stamps are prepared separately for each new row.
    """

    given_values: dict[str, Any]
    columns: tuple[SqlField, ...]
    database_values: tuple[Any, ...]
    assignments: dict[tuple[str, int], BoundWriteValue]
