from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, TypeVar

from hare.fields.constants import CASCADE
from hare.fields.enums import OnDelete, RelationType
from hare.fields.swappable import SwappableModelReference

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance

TModel = TypeVar("TModel", bound="Model")


class OneToOneFieldInstance(ForeignKeyFieldInstance[TModel]):
    relation_type = RelationType.ONE_TO_ONE
    allows_primary_key = True
    allows_unique = True
    # The UNIQUE constraint already indexes the column.
    indexed_by_default = False

    def __init__(
        self,
        model_name: type[TModel] | str | SwappableModelReference,
        related_name: str | None | Literal[False] = None,
        on_delete: OnDelete = CASCADE,
        **kwargs: Any,
    ) -> None:
        super().__init__(model_name, related_name, on_delete, unique=True, **kwargs)

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        path, args, kwargs = super().deconstruct()
        # unique=True is set implicitly by OneToOneField.__init__
        kwargs.pop("unique", None)
        return path, args, kwargs


OneToOneNullableRelation = OneToOneFieldInstance[TModel] | None
"""
Type hint for the result of accessing the :func:`.OneToOneField` field in the model
when obtained model can be nullable.
"""


OneToOneRelation = OneToOneFieldInstance[TModel]
"""
Type hint for the result of accessing the :func:`.OneToOneField` field in the model.
"""
