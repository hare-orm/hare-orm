from unittest.mock import AsyncMock, patch

import pytest

from hare.contrib.test import init_memory_database
from hare.core.config import HareConfig

pytestmark = [pytest.mark.database_independent, pytest.mark.asyncio]


@pytest.mark.parametrize(
    ("models", "expected_modules"),
    [(None, ["__main__"]), (["app.models"], ["app.models"]), ("app.models", ["app.models"])],
    ids=["bare-decorator", "module-list", "module-string"],
)
@patch("hare.Hare.init")
@patch("hare.Hare.generate_schemas")
async def test_init_memory_database_decorator(
    mocked_generate: AsyncMock,
    mocked_init: AsyncMock,
    models: str | list[str] | None,
    expected_modules: list[str],
) -> None:
    async def run():
        return "result"

    decorated = init_memory_database(run) if models is None else init_memory_database(models)(run)
    result = await decorated()
    assert result == "result"
    mocked_init.assert_awaited_once_with(
        HareConfig.from_db_url("sqlite+aiosqlite://:memory:", {"models": expected_modules})
    )
    mocked_generate.assert_awaited_once()
