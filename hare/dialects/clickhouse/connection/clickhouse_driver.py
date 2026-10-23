from __future__ import annotations

from hare.dialects.base.connection.driver import Driver
from hare.dialects.clickhouse.client.clickhouse_error_codes import ClickhouseErrorCodes
from hare.dialects.clickhouse.client.constants import (
    CLICKHOUSE_SERIALIZATION_ERROR_CODE,
    CLICKHOUSE_SERIALIZATION_ERROR_MESSAGE,
    CLICKHOUSE_UNFINISHED_ERROR_CODE,
)
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
        # A transaction's statement meeting a part another transaction (a background merge) holds - a
        # serialization error, which a mutation reports wrapped in its own failure.
        message = str(error)
        code = ClickhouseErrorCodes.get_error_code(message)
        return code == CLICKHOUSE_SERIALIZATION_ERROR_CODE or (
            code == CLICKHOUSE_UNFINISHED_ERROR_CODE and CLICKHOUSE_SERIALIZATION_ERROR_MESSAGE in message
        )
