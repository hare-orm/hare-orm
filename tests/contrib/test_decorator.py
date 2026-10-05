from unittest.mock import AsyncMock, patch

import pytest

from hare.contrib.test import init_memory_sqlite, requires_features
from hare.core.config import HareConfig


@pytest.mark.asyncio
@requires_features(dialect="sqlite")
@patch("hare.Hare.init")
@patch("hare.Hare.generate_schemas")
async def test_init_memory_sqlite_decorator(
    mocked_generate: AsyncMock,
    mocked_init: AsyncMock,
    db,
) -> None:
    """Test init_memory_sqlite as decorator without parentheses."""

    @init_memory_sqlite
    async def run():
        return "result"

    result = await run()
    assert result == "result"
    mocked_init.assert_awaited_once_with(
        HareConfig.from_db_url("sqlite+aiosqlite://:memory:", {"models": ["__main__"]})
    )
    mocked_generate.assert_awaited_once()


@pytest.mark.asyncio
@requires_features(dialect="sqlite")
@patch("hare.Hare.init")
@patch("hare.Hare.generate_schemas")
async def test_init_memory_sqlite_decorator_with_models_list(
    mocked_generate: AsyncMock,
    mocked_init: AsyncMock,
    db,
) -> None:
    """Test init_memory_sqlite as decorator with models list."""

    @init_memory_sqlite(["app.models"])
    async def run():
        return "result"

    result = await run()
    assert result == "result"
    mocked_init.assert_awaited_once_with(
        HareConfig.from_db_url("sqlite+aiosqlite://:memory:", {"models": ["app.models"]})
    )
    mocked_generate.assert_awaited_once()


@pytest.mark.asyncio
@requires_features(dialect="sqlite")
@patch("hare.Hare.init")
@patch("hare.Hare.generate_schemas")
async def test_init_memory_sqlite_decorator_with_models_string(
    mocked_generate: AsyncMock,
    mocked_init: AsyncMock,
    db,
) -> None:
    """Test init_memory_sqlite as decorator with models string."""

    @init_memory_sqlite("app.models")
    async def run():
        return "result"

    result = await run()
    assert result == "result"
    mocked_init.assert_awaited_once_with(
        HareConfig.from_db_url("sqlite+aiosqlite://:memory:", {"models": ["app.models"]})
    )
    mocked_generate.assert_awaited_once()
