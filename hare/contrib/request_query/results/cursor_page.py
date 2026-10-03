from __future__ import annotations

import dataclasses


@dataclasses.dataclass(slots=True)
class CursorPage[ItemType]:
    """One page of a ``CursorPagination``.

    Attributes:
        result: The rows of the page.
        limit: The page size.
        next_cursor: The cursor of the next page, None on the last one.
        previous_cursor: The cursor of the previous page, None on the first one.
        next: The address of the next page, None on the last one or without a request address.
        previous: The address of the previous page, None on the first one or without a request
            address.
    """

    result: list[ItemType]
    limit: int
    next_cursor: str | None
    previous_cursor: str | None
    next: str | None
    previous: str | None
