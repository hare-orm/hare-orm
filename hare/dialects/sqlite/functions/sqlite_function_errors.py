from __future__ import annotations

from collections.abc import Callable
from typing import Any


class SqliteFunctionErrors:
    """The message of the last error one of hare's functions raised on a SQLite connection - SQLite
    reports every one as ``user-defined function raised exception``, and hare's error gives the
    function's own message instead. A native function is given its Python function wrapped, which
    alone raises, so a call that succeeds costs nothing more."""

    __slots__ = ("message",)

    def __init__(self) -> None:
        #: The message of the last error, None once taken.
        self.message: str | None = None

    def wrap(self, function: Callable[..., Any]) -> Callable[..., Any]:
        """A function keeping the message of an error it raises.

        Args:
            function: The function.

        Returns:
            The function wrapped.
        """

        def call(*arguments: Any) -> Any:
            try:
                return function(*arguments)
            except Exception as error:
                self.message = str(error)
                raise

        return call

    def take_message(self) -> str | None:
        """The message of the last error, forgotten.

        Returns:
            The message, None without an error since it was last taken.
        """
        message, self.message = self.message, None
        return message
