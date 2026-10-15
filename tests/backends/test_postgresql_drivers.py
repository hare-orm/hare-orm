"""The PostgreSQL drivers: one engine name and DB_URL scheme each, and a clean error when the
rust.pg extension isn't built."""

import subprocess
import sys
import textwrap

from hare.dialects.dialect_registry import DialectRegistry
from hare.dialects.postgresql.constants import POSTGRESQL_DIALECT
from hare.dialects.postgresql.drivers.asyncpg.client import AsyncpgClient


def test_rust_pg_driver_raises_configuration_error_when_the_extension_is_missing() -> None:
    """`import rust.pg` succeeds against the crate's source directory even when the extension
    isn't built (rust/ is a namespace package), so the client module guards it itself. Run in a
    fresh process so nothing imported the real rust.pg first."""
    script = textwrap.dedent(
        """
        import sys
        from unittest import mock

        sys.modules["rust.native"] = mock.MagicMock(spec=[])

        from hare.dialects.dialect_registry import DialectRegistry
        from hare.exceptions import ConfigurationError

        try:
            DialectRegistry.get_driver("postgresql").get_client_class({})
        except ConfigurationError as exc:
            assert "rust.native extension" in str(exc), str(exc)
        else:
            raise AssertionError("get_client_class did not raise ConfigurationError")
        print("OK")
        """
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


def test_each_driver_has_one_engine_name_and_url_scheme() -> None:
    asyncpg_driver = DialectRegistry.get_driver("postgresql+asyncpg")
    rust_pg_driver = DialectRegistry.get_driver("postgresql")
    assert asyncpg_driver.get_client_class({}) is AsyncpgClient
    assert (asyncpg_driver.url_schemes, rust_pg_driver.url_schemes) == (("postgresql+asyncpg",), ("postgresql",))
    assert asyncpg_driver.dialect is rust_pg_driver.dialect is POSTGRESQL_DIALECT
    assert DialectRegistry.get_driver_for_url_scheme("postgresql") is rust_pg_driver
