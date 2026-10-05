from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ColumnMismatch:
    """A column whose definition differs from its field's in a way no operation can describe."""

    app_label: str
    model_name: str
    table: str
    column: str
    detail: str
