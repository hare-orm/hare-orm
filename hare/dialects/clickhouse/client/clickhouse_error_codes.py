from __future__ import annotations

from hare.dialects.clickhouse.client.constants import CLICKHOUSE_ERROR_CODE_PREFIX


class ClickhouseErrorCodes:
    """The ClickHouse error code a driver's error message names."""

    @staticmethod
    def get_error_code(message: str) -> int | None:
        """The code ``message`` names after ``Code:``.

        Args:
            message: The error's message.

        Returns:
            The code, None when the message names none.
        """
        start = message.find(CLICKHOUSE_ERROR_CODE_PREFIX)
        if start == -1:
            return None
        start += len(CLICKHOUSE_ERROR_CODE_PREFIX)
        end = start
        while end < len(message) and message[end].isdigit():
            end += 1
        return int(message[start:end]) if end > start else None
