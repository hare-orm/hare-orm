from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, TypeVar

from hare.fields.constants import CASCADE
from hare.fields.enums import OnDelete, RelationLoadStrategy
from hare.fields.relations.swappable_model_reference import SwappableModelReference

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
    from hare.query.queryset.relations.many_to_many_relation import ManyToManyRelation
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance

TModel = TypeVar("TModel", bound="Model")


def ManyToManyField(
    to: type[TModel] | str | SwappableModelReference,
    through: str | type[Model] | SwappableModelReference | None = None,
    forward_key: str | None = None,
    backward_key: str = "",
    related_name: str = "",
    on_delete: OnDelete = CASCADE,
    db_constraint: bool = True,
    unique: bool = True,
    *,
    lazy: Literal[RelationLoadStrategy.SELECT] | None = None,
    **kwargs: Any,
) -> ManyToManyRelation[TModel]:
    """A many-to-many relation to another model.

    Args:
        to: The related model, or its name as ``"app.Model"``.
        through: The through table's name, or a model declared with a ``ForeignKeyField`` to each
            side and fields of its own - hare then manages it as a model, and it stays queryable.
            ``.add()`` writes only the key columns, so its other fields need ``null=True`` or a
            ``db_default``.
        forward_key: The through table's column referencing the related model.
        backward_key: The through table's column referencing this model.
        related_name: The attribute on the related model reading the reverse relation.
        db_constraint: Whether the database gets foreign key constraints - True by default.
        on_delete: ``CASCADE`` (default), ``RESTRICT``, ``SET_NULL``, ``SET_DEFAULT`` or
            ``NO_ACTION`` for the through table's foreign keys. Not ``PROTECT``.
        unique: Whether the through table gets a unique index over both keys - True by default.
        lazy: Only ``RelationLoadStrategy.SELECT`` - an implicit ``.prefetch_related(field)``. None by
            default.
        kwargs: The options of every field (``description``, ...).
    """
    return ManyToManyFieldInstance(  # type: ignore[return-value]
        to,
        through,
        forward_key,
        backward_key,
        related_name,
        on_delete=on_delete,
        db_constraint=db_constraint,
        unique=unique,
        lazy=lazy,
        **kwargs,
    )
