from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from clickhouse_connect.driver.asyncclient import AsyncClient
from clickhouse_connect.driver.external import ExternalData

from hare.dialects.clickhouse.client.clickhouse_statement_result import ClickhouseStatementResult
from hare.dialects.clickhouse.drivers.clickhouse_connect.client.clickhouse_json_compact_values import (
    ClickhouseJsonCompactValues,
)
from hare.dialects.clickhouse.drivers.clickhouse_connect.constants import (
    CLICKHOUSE_CONNECT_TOTALS_FORMAT,
    CLICKHOUSE_CONNECT_TOTALS_SETTINGS,
)


class ClickhouseConnectTotalsQuery:
    """A read of ``GROUP BY ... WITH TOTALS`` through clickhouse-connect - in ``JSONCompact``, whose
    ``totals`` holds the row the server sends in no ``Native`` response over HTTP."""

    @classmethod
    async def run(
        cls, library_client: AsyncClient, sql: str, external_data: ExternalData | None, rows_by_position: bool
    ) -> ClickhouseStatementResult:
        """Runs the read.

        Args:
            library_client: The library client.
            sql: The statement.
            external_data: The external tables it reads.
            rows_by_position: Whether the rows are read by position - else by column name.

        Returns:
            The rows and the totals row.
        """
        body = await library_client.raw_query(
            sql,
            settings=CLICKHOUSE_CONNECT_TOTALS_SETTINGS,
            fmt=CLICKHOUSE_CONNECT_TOTALS_FORMAT,
            external_data=external_data,
        )
        response = json.loads(body, parse_float=Decimal)
        columns = [(column["name"], column["type"]) for column in response["meta"]]
        rows = [cls.read_row(columns, values, rows_by_position) for values in response["data"]]
        result = ClickhouseStatementResult(len(rows), rows, description=tuple(columns) if rows_by_position else None)
        totals = response.get("totals")
        if totals is not None:
            result.totals = cls.read_row(columns, totals, rows_by_position)
        return result

    @staticmethod
    def read_row(columns: list[tuple[str, str]], values: list[Any], rows_by_position: bool) -> Any:
        """A row of the response.

        Args:
            columns: The name and the type of each column.
            values: The row's JSON values.
            rows_by_position: Whether the row is read by position - else by column name.

        Returns:
            The row - a tuple, or a dict by column name.
        """
        row = tuple(
            ClickhouseJsonCompactValues.get_value(type_name, value)
            for (_name, type_name), value in zip(columns, values, strict=True)
        )
        return row if rows_by_position else dict(zip((name for name, _type_name in columns), row, strict=True))
