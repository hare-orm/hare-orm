from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.dialects.clickhouse.client.clickhouse_error_codes import ClickhouseErrorCodes
from hare.dialects.clickhouse.client.constants import (
    CLICKHOUSE_AUTHENTICATION_ERROR_CODES,
    CLICKHOUSE_AUTHENTICATION_FAILED_MESSAGE,
    CLICKHOUSE_BAD_TTL_EXPRESSION_ERROR_CODE,
    CLICKHOUSE_CANNOT_CONVERT_TYPE_ERROR_CODE,
    CLICKHOUSE_CONNECTION_ERROR_CODES,
    CLICKHOUSE_INTEGRITY_ERROR_CODES,
    CLICKHOUSE_JOINED_CORRELATED_COLUMN_HINT,
    CLICKHOUSE_JOINED_CORRELATED_COLUMN_MESSAGE,
    CLICKHOUSE_MISSING_DATABASE_MESSAGE,
    CLICKHOUSE_NOT_IMPLEMENTED_ERROR_CODE,
    CLICKHOUSE_NULL_INTO_NOT_NULL_MESSAGE,
    CLICKHOUSE_NULLABLE_TTL_EXPRESSION_HINT,
    CLICKHOUSE_NULLABLE_TYPE_MARK,
    CLICKHOUSE_UNKNOWN_DATABASE_ERROR_CODE,
    CLICKHOUSE_UNKNOWN_DATABASE_TEXT,
)
from hare.exceptions import ConfigurationError, DBConnectionError, HareError, IntegrityError, UnSupportedError

if TYPE_CHECKING:
    from hare.dialects.clickhouse.client.clickhouse_client import ClickhouseClient


class ClickhouseErrors:
    """Translates ClickHouse server and driver failures using their codes and connection settings."""

    @staticmethod
    def get_error_code(error: BaseException) -> int | None:
        """The ClickHouse error code an error's message names.

        Args:
            error: The error.

        Returns:
            The code, None when the message names none.
        """
        return ClickhouseErrorCodes.get_error_code(str(error))

    @staticmethod
    def get_hare_error(client: ClickhouseClient, error: Exception, sql: Any, parameters: Any) -> Exception:
        """hare's exception for a driver's.

        Args:
            error: The driver's exception.
            sql: The statement.
            parameters: Its values.

        Returns:
            The exception to raise.
        """
        code = client.get_error_code(error)
        # An error the server reported its code of is the server's, though a driver reports one in the
        # middle of a stream as a failed connection.
        if code is None and isinstance(error, client.driver_errors.connection):
            return DBConnectionError(error, sql=sql, parameters=parameters)
        if code in CLICKHOUSE_INTEGRITY_ERROR_CODES or (
            code == CLICKHOUSE_CANNOT_CONVERT_TYPE_ERROR_CODE and CLICKHOUSE_NULL_INTO_NOT_NULL_MESSAGE in str(error)
        ):
            # A NULL written into a NOT NULL column is the IntegrityError it is on every database.
            return IntegrityError(error, sql=sql, parameters=parameters)
        if code == CLICKHOUSE_BAD_TTL_EXPRESSION_ERROR_CODE and CLICKHOUSE_NULLABLE_TYPE_MARK in str(error):
            return UnSupportedError(f"{error} - {CLICKHOUSE_NULLABLE_TTL_EXPRESSION_HINT}")
        if code == CLICKHOUSE_NOT_IMPLEMENTED_ERROR_CODE:
            # A statement the server can't run yet - a correlated subquery of a shape it doesn't take.
            if CLICKHOUSE_JOINED_CORRELATED_COLUMN_MESSAGE in str(error):
                return UnSupportedError(f"{error} - {CLICKHOUSE_JOINED_CORRELATED_COLUMN_HINT}")
            return UnSupportedError(f"{error} (sql={sql!r})")
        if code in CLICKHOUSE_CONNECTION_ERROR_CODES:
            return DBConnectionError(error, sql=sql, parameters=parameters)
        if code in CLICKHOUSE_AUTHENTICATION_ERROR_CODES:
            return ConfigurationError(CLICKHOUSE_AUTHENTICATION_FAILED_MESSAGE.format(user=client.user, error=error))
        if (
            code == CLICKHOUSE_UNKNOWN_DATABASE_ERROR_CODE
            and client.database
            and CLICKHOUSE_UNKNOWN_DATABASE_TEXT.format(database=client.database) in str(error)
        ):
            return ConfigurationError(
                CLICKHOUSE_MISSING_DATABASE_MESSAGE.format(database=client.database, error=error)
            )
        return client._get_operational_error(error, sql=sql, parameters=parameters)

    @staticmethod
    def translate_driver_error(
        client: Any,
        error: BaseException,
        sql: str | None,
        parameters: Any,
        args: tuple[Any, ...],
        is_query_executing: bool,
    ) -> BaseException:
        if isinstance(error, HareError) or not isinstance(error, client.driver_errors.driver):
            return error
        return client.get_hare_error(error, sql, parameters)
