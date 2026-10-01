from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class CursorPageSchema[RowSchema: BaseModel](BaseModel):
    """A ``CursorPage`` of a ``CursorPagination``.

    Attributes:
        result: The rows of the page.
        limit: The page size.
        next_cursor: The cursor of the next page.
        previous_cursor: The cursor of the previous page.
        next: The address of the next page.
        previous: The address of the previous page.
    """

    model_config = ConfigDict(from_attributes=True)

    result: list[RowSchema]
    limit: int
    next_cursor: str | None
    previous_cursor: str | None
    next: str | None
    previous: str | None
