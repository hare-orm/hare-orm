from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeAlias

from hare.fields import Field

if TYPE_CHECKING:
    from hare.fields.base.field import Field as BaseField
    from hare.query.queryset.relations.many_to_many_relation import ManyToManyRelation

    #: What a field operation carries - a model field, an M2M relation, or nothing.
    FieldLike: TypeAlias = BaseField[Any] | ManyToManyRelation[Any] | None
else:
    FieldLike = Field
