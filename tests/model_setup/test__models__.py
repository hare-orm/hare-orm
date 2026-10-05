"""
Tests for __models__
"""

import os
import re
from unittest.mock import AsyncMock, patch

import pytest

from hare import Connections, Hare
from hare.core.hare_context import HareContext
from hare.dialects.base.connection.db_url_config_generator import DbUrlConfigGenerator


async def _reset_hare():
    """Helper to reset Hare state before each test."""
    ctx = HareContext.get_current()
    if ctx is not None:
        if ctx._connections is not None:
            ctx._connections._storage.clear()
            ctx._connections._db_config = None
            ctx._connections = None
        ctx._apps = None
        ctx._inited = False
        ctx._default_connection = None


def _get_engine() -> str:
    """Get the current test engine."""
    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    config = DbUrlConfigGenerator.build(db_url, app_modules={"models": []}, connection_label="models")
    return config["connections"]["models"]["engine"]


async def _init_for(module: str, safe: bool = False) -> list[str]:
    """
    Initialize Hare for a specific module and return SQL statements.

    Raises SkipTest if not using sqlite.
    """
    engine = _get_engine()
    if engine != "sqlite":
        pytest.skip("sqlite only")

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
    sqls = await _init_for("tests.model_setup.models__models__good")
    sql_joined = "; ".join(sqls)
    assert "goodtournament" in sql_joined
    assert "inaclasstournament" in sql_joined
    assert "badtournament" not in sql_joined


@pytest.mark.asyncio
async def test_bad():
    await _reset_hare()
    sqls = await _init_for("tests.model_setup.models__models__bad")
    sql_joined = "; ".join(sqls)
    assert "goodtournament" not in sql_joined
    assert "inaclasstournament" not in sql_joined
    assert "badtournament" in sql_joined
