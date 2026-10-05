from __future__ import annotations

from typing import TYPE_CHECKING, Any, Self, TypeVar, cast

from hare.query.queryset.relations.many_to_many.linked_rows_queries import LinkedRowsQueries
from hare.query.queryset.relations.many_to_many_relation import ManyToManyRelation
from hare.query.queryset.relations.related_queryset.unsaved_owner_condition import UnsavedOwnerCondition
from hare.query.queryset.specification_copying import SpecificationCopying

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model

TModel = TypeVar("TModel", bound="Model")


class LinkedManyToManyRelation(LinkedRowsQueries, ManyToManyRelation[TModel]):  # type: ignore[misc]
    """The relation of a ``ManyToManyField`` to a model on another connection: its rows are read by
    the keys its through table links them by - no query joins the two connections' tables."""

    __slots__ = ()

    @property
    def linked_relation(self) -> ManyToManyRelation[Any]:
        """The relation whose links its queries read - itself; a queryset made from it holds it."""
        return self

    def _bind(self) -> None:
        """Makes the query settings - the related model manager's, with no condition on the relation:
        each query adds the keys it reads."""
        manager = self.model._meta.manager
        queryset = manager.get_queryset()
        if not self.instance._saved_in_db:
            queryset._q_objects.append(UnsavedOwnerCondition())
        self._copy_bound_settings(queryset, None)

    def _clone(self) -> Self:
        linked_class = LinkedRowsQueries.get_class_for(self.model._meta.manager.queryset_class)
        queryset = linked_class.__new__(linked_class)
        SpecificationCopying.compile_copy(linked_class)(self, queryset)
        queryset._prefetch_queries = {key: list(value) for key, value in self._prefetch_queries.items()}
        return cast("Self", queryset)
