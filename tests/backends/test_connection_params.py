from unittest.mock import ANY, AsyncMock, patch

import asyncpg
import pytest

from hare.core.hare_context import HareContext
from hare.dialects.postgresql.drivers.asyncpg.client import AsyncpgClient
from hare.exceptions import ConfigurationError


@pytest.mark.asyncio
async def test_asyncpg_connection_params():
    try:
        with (
            patch(
                "hare.dialects.postgresql.drivers.asyncpg.client.asyncpg_client.asyncpg.create_pool", new=AsyncMock()
            ) as asyncpg_connect,
            patch.object(AsyncpgClient, "_fetch_server_version_number", new=AsyncMock(return_value=180000)),
        ):
            ctx = HareContext()
            async with ctx:
                await ctx.connections._init(
                    {
                        "models": {
                            "engine": "postgresql+asyncpg",
                            "credentials": {
                                "database": "test",
                                "host": "127.0.0.1",
                                "password": "foomip",
                                "port": 5432,
                                "user": "root",
                                "timeout": 30,
                                "ssl": True,
                            },
                        }
                    },
                    False,
                )
                await ctx.connections.get("models").create_connection(with_db=True)

                asyncpg_connect.assert_awaited_once_with(  # nosec
                    None,
                    database="test",
                    host="127.0.0.1",
                    password="foomip",
                    port=5432,
                    ssl=True,
                    timeout=30,
                    user="root",
                    max_size=16,
                    min_size=1,
                    connection_class=asyncpg.connection.Connection,
                    loop=None,
                    server_settings={"TimeZone": "UTC"},
                    reset=AsyncpgClient._skip_default_reset,
                    init=ANY,
                    connect=ANY,
                )
    except ImportError:
        pytest.skip("asyncpg not installed")


@pytest.mark.asyncio
@pytest.mark.parametrize("ssl_credential", ["ssl", "sslmode"])
async def test_rust_pg_rejects_asyncpg_style_ssl_credential(ssl_credential):
    """rust_pg only reads ssl_mode - an "ssl"/"sslmode" credential was dropped silently and the
    connection went out without TLS."""
    rust_pg_client_module = pytest.importorskip("hare.dialects.postgresql.drivers.rust_pg.client")
    with pytest.raises(ConfigurationError, match="ssl_mode"):
        rust_pg_client_module.RustPgClient(
            connection_alias="models", host="127.0.0.1", database="test", **{ssl_credential: "require"}
        )
