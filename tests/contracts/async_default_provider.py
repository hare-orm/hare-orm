from typing import Any, ClassVar


class AsyncDefaultProvider:
    """An async callable object used directly or through partial application."""

    calls: ClassVar[list[Any]] = []

    async def __call__(self, value: Any = 17) -> Any:
        self.calls.append(value)
        return value
