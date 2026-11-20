"""A model operation must reject an unsuitable tenant client before sending statements."""

from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from hare.contrib.test import capture_queries


class TenantConnectionAssertions:
    @staticmethod
    async def refused(operation: Callable[[], Awaitable[Any]], error: type[Exception]) -> None:
        async with capture_queries() as captured:
            with pytest.raises(error):
                await operation()
        assert captured.count == 0

    @staticmethod
    async def iterate(queryset):
        return [obj async for obj in queryset.iterator(chunk_size=1)]
