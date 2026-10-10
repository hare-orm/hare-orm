from __future__ import annotations

from hare.dialects.base.connection.driver import Driver
from hare.dialects.clickhouse.constants import (
    CLICKHOUSE_CONNECTION_OPTIONS,
    CLICKHOUSE_DEFAULT_HTTP_PORT,
    CLICKHOUSE_DIALECT,
)


class ClickhouseDriver(Driver):
    """What every ClickHouse driver shares: the DB_URL credentials and settings - the URL's path
    names the database."""

    dialect = CLICKHOUSE_DIALECT
    path_credential = "database"
    authority_credentials = {
        "hostname": "host",
        "port": "port",
        "username": "user",
        "password": "password",  # nosec B105 - the name of a credential, not its value
    }
    default_credentials = {"port": CLICKHOUSE_DEFAULT_HTTP_PORT}
    connection_options = CLICKHOUSE_CONNECTION_OPTIONS
    strict_query_parameters = True

    def is_retryable(self, error: BaseException) -> bool:
        # No transactions - nothing is retried as a whole.
        return False
