from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.migrations.state.project.model_state import ModelState
    from hare.models.enums import ModelOption


@dataclass
class InspectedModel:
    """One introspected table as a model: its migration state - the same a declared model has -
    and what only the model's source text carries.

    Attributes:
        class_name: The model class's name.
        table_name: The table.
        state: The model's state - None for a table skipped as a ManyToManyField through table.
        todo_reasons: Why a field is a best-effort reconstruction, by attribute name - written as
            a TODO comment after it.
        meta_layout: The ``Meta`` body in order: an option of ``state.options``, or a comment line.
        skipped_note: The note a skipped table gets instead of a class.
    """

    class_name: str
    table_name: str
    state: ModelState | None
    todo_reasons: dict[str, str] = field(default_factory=dict)
    meta_layout: list[ModelOption | str] = field(default_factory=list)
    skipped_note: str | None = None
