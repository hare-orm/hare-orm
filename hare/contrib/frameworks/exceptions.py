from __future__ import annotations

from typing import Any


class ServerErrorResponse(Exception):
    """A request answered with a server error - its transaction rolls back as it would for an
    exception.

    Args:
        status_code: The response's status code.
    """

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"The response is a server error ({status_code})")

    def __reduce__(self) -> tuple[type[ServerErrorResponse], tuple[Any, ...]]:
        return self.__class__, (self.status_code,)
