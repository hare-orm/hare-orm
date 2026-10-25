from __future__ import annotations

import dataclasses

from hare.query.enums import Lookup


@dataclasses.dataclass(frozen=True, slots=True)
class Filter:
    """The filter a parameter applies, when it isn't the parameter's own name - an alias that keeps
    the model's structure out of the API::

        publisher_ids: Annotated[list[UUID] | None, Filter("author__publisher", lookup="in")] = None

    Args:
        key: The ``.filter()`` key - a field, a path through relations, optionally with its lookup.
        lookup: A lookup appended to ``key`` (``"in"`` makes ``key__in``); None when ``key``
            already names it or compares with equality.
    """

    key: str
    lookup: Lookup | str | None = None

    @property
    def filter_key(self) -> str:
        """The ``.filter()`` key the parameter's value goes to."""
        return self.key if self.lookup is None else f"{self.key}__{self.lookup}"
