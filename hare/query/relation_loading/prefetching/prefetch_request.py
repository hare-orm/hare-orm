from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.dialects.base.clauses.enums import RowLockStrength
from hare.query.relation_loading.prefetching.prefetch import Prefetch
from hare.query.scopes.row_visibility import RowVisibility

if TYPE_CHECKING:
    from hare.query.queryset import QuerySet
    from hare.query.statements.select.model_rows_query import ModelRowsQuery


@dataclass(frozen=True, slots=True)
class PrefetchRequest:
    """What a query prefetches for the instances it loads, and the settings the prefetch queries take
    over: its visibility and tenant, its ``select_for_update()`` lock, and whether its connection
    was pinned with ``.using()``.
    """

    #: Relation or to_attribute name -> dependent relation lookups.
    prefetch_map: dict[str, set[str | Prefetch]]
    #: Relation name -> ``(to_attribute, queryset)`` of each explicit ``Prefetch()``.
    prefetch_queries: dict[str, list[tuple[str | None, QuerySet[Any]]]]
    visibility: RowVisibility = RowVisibility.DEFAULT
    select_for_update: bool = False
    select_for_update_nowait: bool = False
    select_for_update_skip_locked: bool = False
    select_for_update_strength: RowLockStrength | None = None
    db_explicitly_chosen: bool = False

    @classmethod
    def from_query(
        cls,
        query: QuerySet[Any] | ModelRowsQuery[Any],
        prefetch_map: dict[str, set[str | Prefetch]],
        prefetch_queries: dict[str, list[tuple[str | None, QuerySet[Any]]]],
    ) -> PrefetchRequest:
        """Capture a query's visibility, row lock and connection choice for explicit prefetch maps.

        The maps can describe the query's relations or dependencies of a custom ``to_attribute``;
        they are retained without mutation.
        """
        return cls(
            prefetch_map,
            prefetch_queries,
            visibility=query._visibility,
            select_for_update=query._select_for_update,
            select_for_update_nowait=query._select_for_update_nowait,
            select_for_update_skip_locked=query._select_for_update_skip_locked,
            select_for_update_strength=query._select_for_update_strength,
            db_explicitly_chosen=query._connection_explicitly_chosen,
        )
