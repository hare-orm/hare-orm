from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hare.models.model import Model
    from hare.query.execution.write_statements import WriteCommand


@dataclass(frozen=True, slots=True)
class BoundInsertBatch:
    """A bound command and the objects/columns needed to apply its returned rows.

    Matching metadata belongs to this batch, so result handling never recalculates
    conflict groups or reads parameters from objects changed by an earlier result.
    """

    command: WriteCommand
    objects: Sequence[Model]
    populate_returned_fields: bool = False
    conflict_fields: tuple[str, ...] = ()
    written_fields: tuple[str, ...] = ()
    applied_columns: tuple[str, ...] | None = None
    inline_statements: tuple[str, ...] = ()
