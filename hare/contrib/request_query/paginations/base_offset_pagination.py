from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field

from hare.contrib.request_query.enums import ParameterType
from hare.contrib.request_query.options.parameter_field import ParameterField
from hare.contrib.request_query.paginations.constants import DEFAULT_OFFSET_PARAMETER

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.contrib.request_query.request_query import RequestQuery
from hare.contrib.request_query.paginations.pagination import Pagination


@dataclasses.dataclass(frozen=True, slots=True)
class BaseOffsetPagination(Pagination):
    """What the paginations by ``limit`` and ``offset`` share: the offset parameter and the links
    to the next and previous page.

    Args:
        offset_parameter: The name of the offset parameter.
    """

    offset_parameter: str = DEFAULT_OFFSET_PARAMETER

    def get_parameter_fields(self) -> tuple[ParameterField, ...]:
        return (
            *Pagination.get_parameter_fields(self),
            ParameterField(self.offset_parameter, Annotated[int, Field(ge=0)], 0, ParameterType.OFFSET),
        )

    def get_limit_and_offset(self, request_query: RequestQuery[Any]) -> tuple[int, int]:
        """The page size and the offset a request asks for.

        Args:
            request_query: The request query.

        Returns:
            ``(limit, offset)``.
        """
        return getattr(request_query, self.limit_parameter), getattr(request_query, self.offset_parameter)

    def get_next_url(self, request_query: RequestQuery[Any], offset: int, limit: int) -> str | None:
        """The address of the page after the request's.

        Args:
            request_query: The request query.
            offset: The request's offset.
            limit: The page size.

        Returns:
            The address, None without a request address.
        """
        return self.get_url_with(request_query, {self.offset_parameter: str(offset + limit)})

    def get_previous_url(self, request_query: RequestQuery[Any], offset: int, limit: int) -> str | None:
        """The address of the page before the request's - from offset 0 when the request's offset
        is smaller than the page size.

        Args:
            request_query: The request query.
            offset: The request's offset.
            limit: The page size.

        Returns:
            The address, None on the first page or without a request address.
        """
        if offset <= 0:
            return None
        previous_offset = max(0, offset - limit)
        return self.get_url_with(
            request_query, {self.offset_parameter: str(previous_offset) if previous_offset else None}
        )
