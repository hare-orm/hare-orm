from __future__ import annotations

from typing import Any

from hare.classes.constants import CONTAINER_MAX_DEPTH, CONTAINER_NESTING_MESSAGE
from hare.exceptions import QueryError


class ContainerTraversal:
    """Validate literal container depth and cycles without following SQL or expression objects."""

    @staticmethod
    def check(value: Any, label: str) -> None:
        ancestors: set[int] = set()
        pending: list[tuple[Any, int, bool]] = [(value, 1, False)]
        while pending:
            container, depth, leaving = pending.pop()
            if not isinstance(container, (list, tuple, dict)):
                continue
            identity = id(container)
            if leaving:
                ancestors.remove(identity)
                continue
            if identity in ancestors or depth > CONTAINER_MAX_DEPTH:
                raise QueryError(CONTAINER_NESTING_MESSAGE.format(label=label, limit=CONTAINER_MAX_DEPTH))
            ancestors.add(identity)
            pending.append((container, depth, True))
            items = container.values() if isinstance(container, dict) else container
            pending.extend((item, depth + 1, False) for item in items if isinstance(item, (list, tuple, dict)))
