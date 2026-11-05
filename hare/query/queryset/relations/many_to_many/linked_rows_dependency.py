from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.exceptions import QueryError
from hare.query.constants import UNSAVED_OWNER_MESSAGE
from hare.query.execution.query_dependency import QueryDependency
from hare.query.expressions import Q
from hare.query.queryset.relations.many_to_many.linked_keys import LinkedKeys

if TYPE_CHECKING:
    from hare.query.queryset.relations.many_to_many_relation import ManyToManyRelation


class LinkedRowsDependency(QueryDependency):
    """Read cross-connection through-table keys once before the related query executes."""

    __slots__ = ("relation",)

    def __init__(self, relation: ManyToManyRelation[Any]) -> None:
        self.relation = relation

    async def load(self) -> Q:
        if not self.relation.instance._saved_in_db:
            raise QueryError(UNSAVED_OWNER_MESSAGE)
        return Q(pk__in=await LinkedKeys.read_linked_keys(self.relation))
