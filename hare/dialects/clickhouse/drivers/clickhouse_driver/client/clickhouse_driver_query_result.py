from __future__ import annotations

from typing import Any

from clickhouse_driver.protocol import ServerPacketTypes
from clickhouse_driver.result import QueryResult


class ClickhouseDriverQueryResult(QueryResult):  # type: ignore[misc]  # the library ships no types
    """clickhouse-driver's result of a statement, the totals row of ``GROUP BY ... WITH TOTALS`` kept apart
    - the library adds it to the rows."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        #: The rows of the totals block - one for a statement ``WITH TOTALS``, none for another.
        self.totals_rows: list[Any] = []

    def store(self, packet: Any) -> None:
        if getattr(packet, "type", None) == ServerPacketTypes.TOTALS:
            block = getattr(packet, "block", None)
            if block is not None and block.num_rows:
                self.totals_rows.extend(block.get_rows())
            return
        super().store(packet)
