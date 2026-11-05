from __future__ import annotations

from contextvars import ContextVar, Token
from types import TracebackType
from typing import ClassVar


class PrimaryReads:
    """A block whose every read goes to the connection its model is written through - ``with`` and
    ``async with`` alike (``Routing.using_primary()``). Blocks nest."""

    #: Whether the current task is inside such a block.
    active: ClassVar[ContextVar[frozenset[object]]] = ContextVar("hare_primary_reads", default=frozenset())

    __slots__ = ("_token",)

    def __init__(self) -> None:
        self._token: Token[frozenset[object]] | None = None

    @staticmethod
    def is_active() -> bool:
        from hare.core.hare_context import HareContext

        context = HareContext.get_current()
        return context is not None and context.identity in PrimaryReads.active.get()

    def __enter__(self) -> None:
        from hare.core.hare_context import HareContext

        context = HareContext.require_current()
        self._token = PrimaryReads.active.set(PrimaryReads.active.get() | {context.identity})

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        exception_traceback: TracebackType | None,
    ) -> None:
        if self._token is not None:
            PrimaryReads.active.reset(self._token)
            self._token = None

    async def __aenter__(self) -> None:
        self.__enter__()

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        exception_traceback: TracebackType | None,
    ) -> None:
        self.__exit__(exception_type, exception, exception_traceback)
