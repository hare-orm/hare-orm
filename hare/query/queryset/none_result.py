from __future__ import annotations

from collections.abc import (
    Generator,
)


class NoneAwaitableType:
    """What a relation with nothing on the other side reads as - awaiting it gives None, and it is
    falsy."""

    __slots__ = ()

    def __await__(self) -> Generator[None]:
        yield None

    def __bool__(self) -> bool:
        return False


NoneAwaitable = NoneAwaitableType()
