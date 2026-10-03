from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.dialects.base.connection_option import ConnectionOption
from hare.dialects.base.connection_options import ConnectionOptions
from hare.dialects.enums import ConnectionOptionType
from hare.dialects.postgresql.constants import (
    POSTGRES_CONNECTION_OPTIONS,
    POSTGRES_PORT_OPTION,
    RUST_PG_SSL_MODES,
)
from hare.dialects.postgresql.driver import PostgresqlDriver
from hare.dialects.postgresql.drivers.rust_pg.constants import RUST_PG_CONNECTION_OPTIONS
from hare.dialects.registry import DialectRegistry
from hare.exceptions import ConfigurationError, UnSupportedError

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.postgresql.drivers.rust_pg.client.rust_pg_client import RustPgClient


class RustPgDriver(PostgresqlDriver):
    """PostgreSQL through hare's own Rust driver - the default for ``postgresql://``."""

    name = "postgresql"
    url_schemes = ("postgresql",)
    # The driver passes nothing through, so an unknown parameter is rejected rather than dropped.
    strict_query_parameters = True
    connection_options = (
        POSTGRES_CONNECTION_OPTIONS
        + RUST_PG_CONNECTION_OPTIONS
        + ConnectionOptions(ConnectionOption("host", ConnectionOptionType.TEXT), POSTGRES_PORT_OPTION)
    )

    def get_client_class(self, credentials: dict[str, Any]) -> type[RustPgClient]:
        try:
            from hare.dialects.postgresql.drivers.rust_pg.client.rust_pg_client import RustPgClient
        except ImportError as exc:
            raise ConfigurationError(
                "The postgresql engine needs the rust.native extension, which is not built: build it "
                "(`make build_native`) or use the postgresql+asyncpg engine."
            ) from exc
        return RustPgClient

    def get_client_classes(self) -> tuple[type[DatabaseClient], ...]:
        try:
            from hare.dialects.postgresql.drivers.rust_pg.client.rust_pg_client import RustPgClient
            from hare.dialects.postgresql.drivers.rust_pg.client.rust_pg_transaction_client import (
                RustPgTransactionClient,
            )
        except ImportError:
            return ()
        return RustPgClient, RustPgTransactionClient

    def get_ssl_credentials(self, query_param: str, mode: str | bool) -> dict[str, Any]:
        if mode is True:
            mode = "require"
        elif mode is False:
            mode = "disable"
        if mode not in RUST_PG_SSL_MODES:
            raise UnSupportedError(
                f"{query_param}={mode} is not supported by the {self.name} driver: expected one of "
                f"{sorted(RUST_PG_SSL_MODES)}"
            )
        return {"ssl_mode": mode}


DialectRegistry.register_driver(RustPgDriver())
