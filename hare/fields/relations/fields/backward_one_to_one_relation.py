from __future__ import annotations

from typing import TYPE_CHECKING, TypeVar

from hare.fields.enums import RelationType

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
from hare.fields.relations.fields.backward_fk_relation import BackwardFKRelation

TModel = TypeVar("TModel", bound="Model")


class BackwardOneToOneRelation(BackwardFKRelation[TModel]):
    # A single related row, unlike the plain reverse-FK class this subclasses for everything
    # else - overrides BackwardFKRelation.is_multi_valued back to False.
    relation_type = RelationType.BACKWARD_ONE_TO_ONE
    is_multi_valued = False
