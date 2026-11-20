from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from hare.ddl.indexes.index import Index
    from hare.models.model_binding import ModelConstraint


class GenericSchemaBinding(TypedDict):
    """Schema declarations and their last generic-relation expansion within one binding."""

    declared_constraints: tuple[ModelConstraint, ...]
    declared_indexes: tuple[tuple[str, ...] | Index, ...]
    expanded_constraints: tuple[ModelConstraint, ...]
    expanded_indexes: tuple[tuple[str, ...] | Index, ...]
