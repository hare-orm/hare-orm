from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.dialects.clickhouse.connection.clickhouse_driver import ClickhouseDriver
from hare.dialects.clickhouse.constants import CLICKHOUSE_DEFAULT_NATIVE_PORT
from hare.dialects.clickhouse.drivers.clickhouse_driver.constants import CLICKHOUSE_DRIVER_CONNECTION_OPTIONS
from hare.dialects.clickhouse.drivers.constants import CLICKHOUSE_DRIVER_DRIVER_NAME
from hare.dialects.dialect_registry import DialectRegistry
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient


class ClickhouseDriverDriver(ClickhouseDriver):
    """ClickHouse through the clickhouse-driver library, over the server's native TCP protocol - the
    driver of ``clickhouse+clickhouse-driver://``. The driver is registered without clickhouse-driver
    installed; a connection needs it (``hare-orm[clickhouse-driver]``)."""

    name = CLICKHOUSE_DRIVER_DRIVER_NAME
    url_schemes = (CLICKHOUSE_DRIVER_DRIVER_NAME,)
    default_credentials = {"port": CLICKHOUSE_DEFAULT_NATIVE_PORT}
    connection_options = CLICKHOUSE_DRIVER_CONNECTION_OPTIONS

    def get_client_class(self, credentials: dict[str, Any]) -> type[DatabaseClient]:
        try:
            from hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_client import (
                ClickhouseDriverClient,
            )
        except ImportError as error:
            raise ConfigurationError(
                "The clickhouse+clickhouse-driver engine needs clickhouse-driver: "
                "pip install 'hare-orm[clickhouse-driver]'"
            ) from error
        return ClickhouseDriverClient

    def get_client_classes(self) -> tuple[type[DatabaseClient], ...]:
        try:
            from hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_client import (
                ClickhouseDriverClient,
            )
        except ImportError:
            return ()
        return (ClickhouseDriverClient,)


DialectRegistry.register_driver(ClickhouseDriverDriver())
