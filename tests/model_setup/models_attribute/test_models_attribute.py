"""
Tests for __models__
"""

import re
from unittest.mock import AsyncMock, patch

import pytest

from hare import Connections, Hare
from hare.core.hare_context import HareContext

pytestmark = pytest.mark.database_independent


async def _reset_hare():
    """Helper to reset Hare state before each test."""
    ctx = HareContext.get_current()
    if ctx is not None:
        if ctx._connections is not None:
            ctx._connections._get_storage().clear()
            ctx._connections._db_config = None
            ctx._connections = None
        ctx._apps = None
        ctx._inited = False
        ctx._default_connection = None


async def _init_for(module: str, safe: bool = False) -> list[str]:
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
        return Connections.get("default").get_schema_sql(safe).split(";\n")


def _get_sql(sqls: list[str], text: str) -> str:
    """Get SQL statement containing the given text."""
    return str(re.sub(r"[ \t\n\r]+", " ", [sql for sql in sqls if text in sql][0]))


@pytest.mark.asyncio
async def test_good():
    await _reset_hare()
    sqls = await _init_for("tests.model_setup.models_attribute.listing_good_and_nested_models")
    sql_joined = "; ".join(sqls)
    assert "goodtournament" in sql_joined
    assert "inaclasstournament" in sql_joined
    assert "badtournament" not in sql_joined


@pytest.mark.asyncio
async def test_bad():
    await _reset_hare()
    sqls = await _init_for("tests.model_setup.models_attribute.listing_bad_model")
    sql_joined = "; ".join(sqls)
    assert "goodtournament" not in sql_joined
    assert "inaclasstournament" not in sql_joined
    assert "badtournament" in sql_joined
