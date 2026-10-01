from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

from hare.contrib.request_query.results.page import Page

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.contrib.request_query.base import RequestQuery
from hare.contrib.request_query.base_offset_pagination import BaseOffsetPagination


@dataclasses.dataclass(frozen=True, slots=True)
class OffsetPagination(BaseOffsetPagination):
    """Pages by ``limit`` and ``offset``: a ``Page`` with the number of matching rows and links to
    the next and previous page."""

    async def get_page(self, request_query: RequestQuery[Any]) -> Page[Any]:
        limit, offset = self.get_limit_and_offset(request_query)
        queryset = await request_query.get_filtered_queryset()
        count = await queryset.count()
        ordered = request_query.get_ordered_queryset(queryset)
        items = await request_query.after_fetch(list(await ordered.offset(offset).limit(limit)))
        return Page(
            result=items,
            count=count,
            limit=limit,
            offset=offset,
            next=self.get_next_url(request_query, offset, limit) if offset + limit < count else None,
            previous=self.get_previous_url(request_query, offset, limit),
        )
