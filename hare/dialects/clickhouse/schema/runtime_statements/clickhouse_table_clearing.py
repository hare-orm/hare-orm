from __future__ import annotations

from collections.abc import Sequence

from hare.dialects.base.client.database_client import DatabaseClient
from hare.dialects.base.schema.runtime_statements.table_clearing import TableClearing


class ClickhouseTableClearing(TableClearing):
    """Tables emptied with ``TRUNCATE TABLE`` - a ClickHouse DELETE needs a condition and marks
    every row deleted one by one."""

    __slots__ = ()

    @classmethod
    async def clear_tables(cls, connection: DatabaseClient, quoted_tables: Sequence[str]) -> None:
        if quoted_tables:
            script = "".join(f"TRUNCATE TABLE {quoted_table};\n" for quoted_table in quoted_tables)  # nosec
            await connection.execute_script(script)
