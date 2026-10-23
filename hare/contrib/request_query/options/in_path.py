from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True, slots=True)
class InPath:
    """Marks a parameter read from the route's path, not from the query string - for every
    framework adapter::

        pk: Annotated[KeyColumns[int, int], InPath()]
    """
