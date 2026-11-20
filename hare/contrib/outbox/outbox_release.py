from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from hare.contrib.outbox.constants import OUTBOX_RELEASE_FAILED
from hare.core.resource_completion import ResourceCompletion

if TYPE_CHECKING:
    from types import TracebackType


class OutboxRelease:
    """Finish releasing a resource while preserving a preceding operation failure."""

    def __init__(self, get_release: Callable[[], Callable[[], Awaitable[Any]]]) -> None:
        self._get_release = get_release

    async def __aenter__(self) -> None:
        """Enter the operation whose resource this scope releases."""

    async def __aexit__(
        self,
        _exception_type: type[BaseException] | None,
        exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        await self.finish(self._get_release(), after_error=exception)

    @staticmethod
    async def finish(release: Callable[[], Awaitable[Any]], *, after_error: BaseException | None = None) -> None:
        """Drain the release and report both failures when they are distinct."""
        try:
            await ResourceCompletion.wait(release())
        except BaseException as release_error:
            OutboxRelease.raise_if_both_failed(after_error, release_error)
            raise

    @staticmethod
    def raise_if_both_failed(after_error: BaseException | None, release_error: BaseException) -> None:
        """Preserve the operation error when release failed with a distinct exception."""
        if after_error is not None and release_error is not after_error:
            raise BaseExceptionGroup(OUTBOX_RELEASE_FAILED, [after_error, release_error]) from None
