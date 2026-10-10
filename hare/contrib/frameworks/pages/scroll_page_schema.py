from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ScrollPageSchema[RowSchema: BaseModel](BaseModel):
    """A ``ScrollPage`` of a ``ScrollPagination`` - no count.

    Attributes:
        result: The rows of the page.
        limit: The page size.
        offset: How many matching rows precede the page.
        next: The address of the next page.
        previous: The address of the previous page.
    """

    model_config = ConfigDict(from_attributes=True)

    result: list[RowSchema]
    limit: int
    offset: int
    next: str | None
    previous: str | None
