from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, TypeVar

from hare.fields.constants import CASCADE
from hare.fields.enums import OnDelete, RelationType
from hare.fields.relations.swappable_model_reference import SwappableModelReference

if TYPE_CHECKING:
    from hare.models import Model
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance

TModel = TypeVar("TModel", bound="Model")


class OneToOneFieldInstance(ForeignKeyFieldInstance[TModel]):
    """A one-to-one relation - what ``OneToOneField()`` makes: a unique foreign key."""

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
        # Every one-to-one relation is unique - unique=True is never written.
        kwargs.pop("unique", None)
        return path, args, kwargs


#: The annotation of a nullable ``OneToOneField`` on a model.
OneToOneNullableRelation = OneToOneFieldInstance[TModel] | None

#: The annotation of a ``OneToOneField`` on a model.
OneToOneRelation = OneToOneFieldInstance[TModel]
