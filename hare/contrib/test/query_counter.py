from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.instrumentation.query_executed import QueryExecuted


@dataclass
class QueryCounter:
    """What ``capture_queries()``/``assert_num_queries()`` yields - the count and SQL text of every
    query sent to one connection so far, updated live as the ``async with`` body runs.

    Attributes:
        connection_name: The connection whose queries count.
        count: How many queries ran.
        queries: Their SQL text, in order - query tags included; ``COPY <table> (<columns>) FROM
            STDIN`` for a bulk load.
    """

    connection_name: str
    count: int = 0
    queries: list[str] = dataclass_field(default_factory=list)

    def record(self, event: QueryExecuted) -> None:
        """Counts a query of the counted connection - the observer ``capture_queries()`` installs.

        Args:
            event: The query.
        """
        if event.connection_name != self.connection_name:
            return
        self.count += 1
        self.queries.append(event.sql)
