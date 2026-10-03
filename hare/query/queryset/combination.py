from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from hare.sql.enums import SetOperation

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.queryset.queryset import QuerySet
    from hare.query.relation_loading.prefetch import Prefetch


@dataclass(frozen=True, slots=True)
class Combination:
    """The querysets ``union()``/``intersection()``/``difference()`` combine - the queryset
    holding it returns the combined rows: model instances, or the values the branches select.

    Args:
        branches: The querysets, in order - a branch may itself combine querysets.
        set_operations: The operation combining each branch after the first with everything before
            it, in order.
        prefetched_relations: The relations prefetched on the combined model instances
            (``prefetch_related()``).
    """

    branches: tuple[QuerySet[Any, Any], ...]
    set_operations: tuple[SetOperation, ...]
    prefetched_relations: tuple[str | Prefetch, ...] = ()

    @classmethod
    def of(cls, branches: tuple[QuerySet[Any, Any], ...], set_operation: SetOperation) -> Combination:
        """The branches, each after the first combined with everything before it by
        ``set_operation``.

        Args:
            branches: The querysets.
            set_operation: The operation.

        Returns:
            The combination.
        """
        return cls(branches, (set_operation,) * (len(branches) - 1))

    def with_branches(self, branches: tuple[QuerySet[Any, Any], ...], set_operation: SetOperation) -> Combination:
        """The combination with more branches, each combined with everything before it.

        Args:
            branches: The new branches.
            set_operation: The operation combining each of them.

        Returns:
            The combination.
        """
        return replace(
            self,
            branches=self.branches + branches,
            set_operations=self.set_operations + (set_operation,) * len(branches),
        )
