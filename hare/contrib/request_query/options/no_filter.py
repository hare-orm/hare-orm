from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True, slots=True)
class NoFilter:
    """Marks a parameter the query reads but doesn't filter by - a mode, a flag for the handler::

    mode: Annotated[Mode | None, NoFilter()] = None
    """
