from __future__ import annotations

from contextvars import ContextVar, Token
from types import TracebackType
from typing import ClassVar


class PrimaryReads:
    """A block whose every read goes to the connection its model is written through - ``with`` and
    ``async with`` alike (``Routing.using_primary()``). Blocks nest."""

    #: Whether the current task is inside such a block.
    active: ClassVar[ContextVar[bool]] = ContextVar("hare_primary_reads", default=False)

    __slots__ = ("_token",)

    def __init__(self) -> None:
        self._token: Token[bool] | None = None

    def __enter__(self) -> None:
        self._token = PrimaryReads.active.set(True)

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
