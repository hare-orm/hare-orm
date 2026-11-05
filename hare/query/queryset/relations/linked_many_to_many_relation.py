from __future__ import annotations

from typing import TYPE_CHECKING, TypeVar

from hare.query.execution.dependent_condition import DependentCondition
from hare.query.queryset.relations.many_to_many.linked_rows_dependency import LinkedRowsDependency
from hare.query.queryset.relations.many_to_many_relation import ManyToManyRelation

if TYPE_CHECKING:
    from hare.models import Model

TModel = TypeVar("TModel", bound="Model")


class LinkedManyToManyRelation(ManyToManyRelation[TModel]):
    """A cross-connection relation declared as a query with a through-table input.

    Clones retain the manager's QuerySet class. The common executor loads the linked keys,
    then uses the ordinary read, summary or write statement on the related connection.
    """

    __slots__ = ()

    def _bind(self) -> None:
        queryset = self.model._meta.manager.create_queryset()
        dependency = LinkedRowsDependency(self)
        queryset._execution_dependencies = (*queryset._execution_dependencies, dependency)
        queryset._q_objects.append(DependentCondition(dependency))
        queryset._call_signature = None
        self._copy_bound_settings(queryset, None)
