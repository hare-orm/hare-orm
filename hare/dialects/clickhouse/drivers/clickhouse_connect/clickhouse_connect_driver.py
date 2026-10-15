from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.dialects.clickhouse.connection.clickhouse_driver import ClickhouseDriver
from hare.dialects.clickhouse.drivers.constants import CLICKHOUSE_CONNECT_DRIVER_NAME
from hare.dialects.dialect_registry import DialectRegistry
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient


class ClickhouseConnectDriver(ClickhouseDriver):
    """ClickHouse through clickhouse-connect's asyncio HTTP client - the driver of
    ``clickhouse+clickhouse-connect://``. The driver is registered without clickhouse-connect
    installed; a connection needs it (``hare-orm[clickhouse]``)."""

    name = CLICKHOUSE_CONNECT_DRIVER_NAME
    url_schemes = (CLICKHOUSE_CONNECT_DRIVER_NAME,)

    def get_client_class(self, credentials: dict[str, Any]) -> type[DatabaseClient]:
        try:
            from hare.dialects.clickhouse.drivers.clickhouse_connect.client.clickhouse_connect_client import (
                ClickhouseConnectClient,
            )
        except ImportError as error:
            raise ConfigurationError(
                "The clickhouse engine needs clickhouse-connect: pip install 'hare-orm[clickhouse]'"
            ) from error
        return ClickhouseConnectClient

    def get_client_classes(self) -> tuple[type[DatabaseClient], ...]:
        try:
            from hare.dialects.clickhouse.drivers.clickhouse_connect.client.clickhouse_connect_client import (
                ClickhouseConnectClient,
            )
        except ImportError:
            return ()
        return (ClickhouseConnectClient,)


DialectRegistry.register_driver(ClickhouseConnectDriver())
