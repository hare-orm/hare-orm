from __future__ import annotations

from typing import TYPE_CHECKING, TypeVar

from hare.fields.enums import RelationType
from hare.fields.relations.fields.backward_foreign_key_relation import BackwardForeignKeyRelation

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model

TModel = TypeVar("TModel", bound="Model")


class BackwardOneToOneRelation(BackwardForeignKeyRelation[TModel]):
    """The reverse side of a one-to-one relation - the one row of the other model pointing at a row of this
    one."""

    # A single related row, unlike the plain reverse-FK class this subclasses for everything
    # else - overrides BackwardForeignKeyRelation.is_multi_valued back to False.
    relation_type = RelationType.BACKWARD_ONE_TO_ONE
    is_multi_valued = False
