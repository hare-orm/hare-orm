from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

from hare.contrib.request_query.results.scroll_page import ScrollPage

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.contrib.request_query.base import RequestQuery
from hare.contrib.request_query.base_offset_pagination import BaseOffsetPagination


@dataclasses.dataclass(frozen=True, slots=True)
class ScrollPagination(BaseOffsetPagination):
    """Pages by ``limit`` and ``offset`` without counting the matching rows - for a feed or a long
    list that shows only "next" and "previous": the ``COUNT`` of a large table is saved, and
    whether a next page exists is read from one row fetched past the page. The page is a
    ``ScrollPage``, which has no count."""

    async def get_page(self, request_query: RequestQuery[Any]) -> ScrollPage[Any]:
        limit, offset = self.get_limit_and_offset(request_query)
        queryset = await request_query.get_filtered_queryset()
        rows = list(await request_query.get_ordered_queryset(queryset).offset(offset).limit(limit + 1))
        items = await request_query.after_fetch(rows[:limit])
        return ScrollPage(
            result=items,
            limit=limit,
            offset=offset,
            next=self.get_next_url(request_query, offset, limit) if len(rows) > limit else None,
            previous=self.get_previous_url(request_query, offset, limit),
        )
