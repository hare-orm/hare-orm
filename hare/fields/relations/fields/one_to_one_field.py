from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, TypeVar, overload

from hare.fields.constants import CASCADE
from hare.fields.enums import OnDelete
from hare.fields.swappable import SwappableModelReference

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
from hare.fields.relations.fields.one_to_one_field_instance import (
    OneToOneFieldInstance,
    OneToOneNullableRelation,
    OneToOneRelation,
)

TModel = TypeVar("TModel", bound="Model")


@overload
def OneToOneField(
    to: type[TModel] | str | SwappableModelReference,
    related_name: str | None | Literal[False] = None,
    on_delete: OnDelete = CASCADE,
    db_constraint: bool = True,
    *,
    null: Literal[True],
    **kwargs: Any,
) -> OneToOneNullableRelation[TModel]: ...


@overload
def OneToOneField(
    to: type[TModel] | str | SwappableModelReference,
    related_name: str | None | Literal[False] = None,
    on_delete: OnDelete = CASCADE,
    db_constraint: bool = True,
    null: Literal[False] = False,
    **kwargs: Any,
) -> OneToOneRelation[TModel]: ...


def OneToOneField(
    to: type[TModel] | str | SwappableModelReference,
    related_name: str | None | Literal[False] = None,
    on_delete: OnDelete = CASCADE,
    db_constraint: bool = True,
    null: bool = False,
    **kwargs: Any,
) -> OneToOneRelation[TModel] | OneToOneNullableRelation[TModel]:
    """A one-to-one relation to another model - a foreign key whose column is unique.

    Args:
        to: The related model, or its name as ``"app.Model"``.
        related_name: The attribute on the related model reading the reverse relation.
        on_delete: What happens when the related row is deleted: ``CASCADE`` deletes this row,
            ``RESTRICT`` refuses the delete while a key points at it, ``SET_NULL`` sets NULL (needs
            ``null=True``), ``SET_DEFAULT`` sets the default, ``NO_ACTION`` does nothing. With a
            database constraint, ``SET_DEFAULT`` is run by the database and needs ``db_default``;
            with ``db_constraint=False`` hare runs it with an ``UPDATE``, taking ``default`` first,
            else ``db_default`` (a composite-target relation needs a tuple ``default``). The
            soft-delete cascade resets the same way.
        to_field: The related model's field the key references - its primary key by default.
        db_constraint: Whether the database gets a foreign key constraint - True by default.
        lazy: The loading strategy every query on the model applies unless ``.defer_related()`` opts
            out: ``"joined"`` like ``.select_related(field)``, ``"select"`` like
            ``.prefetch_related(field)``. None by default.
    """

    return OneToOneFieldInstance(to, related_name, on_delete, db_constraint=db_constraint, null=null, **kwargs)
