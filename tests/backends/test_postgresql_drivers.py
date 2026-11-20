"""The PostgreSQL drivers: one engine name and DB_URL scheme each, and a clean error where the native
module has no driver."""

import subprocess
import sys
import textwrap

import pytest

from hare.dialects.dialect_registry import DialectRegistry
from hare.dialects.postgresql.constants import POSTGRESQL_DIALECT

pytestmark = pytest.mark.database_independent


def test_rust_pg_driver_raises_configuration_error_without_the_native_driver() -> None:
    """A native module built without the driver - on Windows, where it has no ``pg`` and ``pool`` - makes
    the native engine name the asyncpg one instead. Run in a fresh process so nothing imported the real
    driver first."""
    script = textwrap.dedent(
        """
        import sys

        import rust.native

        for name in ("rust.native.pg", "rust.native.pool"):
            sys.modules[name] = None

        from hare.dialects.dialect_registry import DialectRegistry
        from hare.exceptions import ConfigurationError

        try:
            DialectRegistry.get_driver("postgresql").get_client_class({})
        except ConfigurationError as exc:
            assert "postgresql+asyncpg" in str(exc), str(exc)
        else:
            raise AssertionError("get_client_class did not raise ConfigurationError")
        print("OK")
        """
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


@pytest.mark.requires("asyncpg")
def test_each_driver_has_one_engine_name_and_url_scheme() -> None:
    from hare.dialects.postgresql.drivers.asyncpg.client import AsyncpgClient

    asyncpg_driver = DialectRegistry.get_driver("postgresql+asyncpg")
    rust_pg_driver = DialectRegistry.get_driver("postgresql")
    assert asyncpg_driver.get_client_class({}) is AsyncpgClient
    assert (asyncpg_driver.url_schemes, rust_pg_driver.url_schemes) == (("postgresql+asyncpg",), ("postgresql",))
    assert asyncpg_driver.dialect is rust_pg_driver.dialect is POSTGRESQL_DIALECT
    assert DialectRegistry.get_driver_for_url_scheme("postgresql") is rust_pg_driver
