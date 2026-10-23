from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class PageSchema[RowSchema: BaseModel](BaseModel):
    """A ``Page`` of an ``OffsetPagination``.

    Attributes:
        result: The rows of the page.
        count: How many rows match the request in total.
        limit: The page size.
        offset: How many matching rows precede the page.
        next: The address of the next page.
        previous: The address of the previous page.
    """

    model_config = ConfigDict(from_attributes=True)

    result: list[RowSchema]
    count: int
    limit: int
    offset: int
    next: str | None
    previous: str | None
