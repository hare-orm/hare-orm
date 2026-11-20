"""
Tests for __models__
"""

from unittest.mock import AsyncMock, patch

import pytest

from hare import Connections, Hare
from hare.core.hare_context import HareContext

pytestmark = pytest.mark.database_independent


async def _init_for(module: str) -> list[str]:
    """
    Initialize Hare for a specific module on an in-memory SQLite database of its own and return its SQL
    statements.
    """
    with patch("hare.dialects.sqlite.drivers.aiosqlite.client.AiosqliteClient.create_connection", new=AsyncMock()):
        await Hare.init(
            {
                "connections": {
                    "default": {
                        "engine": "sqlite+aiosqlite",
                        "credentials": {"file_path": ":memory:"},
                    }
                },
                "apps": {"models": {"models": [module], "default_connection": "default"}},
            }
        )
        return Connections.get("default").get_schema_sql(safe=False).split(";\n")


@pytest.mark.asyncio
async def test_good():
    async with HareContext():
        sqls = await _init_for("tests.model_setup.models_attribute.listing_good_and_nested_models")
        sql_joined = "; ".join(sqls)
        assert "goodtournament" in sql_joined
        assert "inaclasstournament" in sql_joined
        assert "badtournament" not in sql_joined


@pytest.mark.asyncio
async def test_bad():
    async with HareContext():
        sqls = await _init_for("tests.model_setup.models_attribute.listing_bad_model")
        sql_joined = "; ".join(sqls)
        assert "goodtournament" not in sql_joined
        assert "inaclasstournament" not in sql_joined
        assert "badtournament" in sql_joined
