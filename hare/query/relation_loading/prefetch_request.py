from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.query.relation_loading.prefetch import Prefetch
from hare.query.scopes.row_visibility import RowVisibility

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.queryset import QuerySet


@dataclass(frozen=True, slots=True)
class PrefetchRequest:
    """What a query prefetches for the instances it loads, and the settings the prefetch queries take
    over: its visibility and tenant, its ``select_for_update()`` lock, and whether its connection
    was pinned with ``.using()``.
    """

    #: Relation name -> the nested relations prefetched through it.
    prefetch_map: dict[str, set[str | Prefetch]]
    #: Relation name -> ``(to_attr, queryset)`` of each explicit ``Prefetch()``.
    prefetch_queries: dict[str, list[tuple[str | None, QuerySet[Any]]]]
    visibility: RowVisibility = RowVisibility.DEFAULT
    select_for_update: bool = False
    select_for_update_nowait: bool = False
    select_for_update_skip_locked: bool = False
    select_for_update_no_key: bool = False
    db_explicitly_chosen: bool = False
