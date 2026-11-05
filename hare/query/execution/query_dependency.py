from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hare.query.expressions import Q


class QueryDependency:
    """An input obtained before compiling and executing a query.

    A dependency is a declaration, shared by query branches. Its result belongs to one
    execution, never to this object or to a cached statement.
    """

    __slots__ = ()

    async def load(self) -> Q:
        """Read the condition to apply for this execution."""
        raise NotImplementedError
