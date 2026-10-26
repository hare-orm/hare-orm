from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hare.ddl.schema_objects.enum_type import EnumType
    from hare.migrations.state.model_state import ModelState


@dataclass(frozen=True, slots=True)
class OperationUndo:
    """What takes a state back to before an operation: the model states it changed as they were - None
    for one it added - and the extensions, schemas and ``ENUM`` types before it."""

    model_states: dict[tuple[str, str], ModelState | None]
    extensions: frozenset[str]
    schemas: frozenset[str]
    enum_types: dict[tuple[str | None, str], EnumType]
