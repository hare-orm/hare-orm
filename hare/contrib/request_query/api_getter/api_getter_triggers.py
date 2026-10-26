from __future__ import annotations

import dataclasses

from hare.contrib.request_query.api_getter.enums import ApiGetterTrigger


@dataclasses.dataclass(frozen=True, slots=True)
class ApiGetterTriggers:
    """When a source's values are loaded - with the rows a request reads, and after a handler creates or
    updates rows (``RequestQuery.load_api_values(items, ApiGetterTrigger.CREATE)``).

    Args:
        on_read: With the rows of ``fetch()``, ``page()`` and ``get()``.
        on_create: With the rows a handler created.
        on_update: With the rows a handler updated.
    """

    on_read: bool = True
    on_create: bool = False
    on_update: bool = False

    def runs_on(self, trigger: ApiGetterTrigger) -> bool:
        """Whether the values are loaded with the rows of a trigger.

        Args:
            trigger: What the rows come from.
        """
        if trigger is ApiGetterTrigger.CREATE:
            return self.on_create
        if trigger is ApiGetterTrigger.UPDATE:
            return self.on_update
        return self.on_read
