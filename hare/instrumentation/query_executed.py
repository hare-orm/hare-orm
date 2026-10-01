from __future__ import annotations

import dataclasses
from typing import Any, ClassVar


@dataclasses.dataclass(frozen=True, slots=True)
class QueryExecuted:
    """A statement sent to the database - every query-executing call (``execute``,
    ``execute_many``, ``execute_script``, a bulk ``COPY``, a stream), successful or not.

    Attributes:
        sql: The statement text, its query tags included; ``COPY <table> (<columns>) FROM STDIN``
            for a bulk load.
        params: The bound parameters, None when there are none to show.
        duration_ms: How long the call took - for a stream, until it was read to the end or
            closed.
        error: The exception the call ended with, None on success.
        connection_name: The connection it ran on.
    """

    #: Observers of this event can't be narrowed to models.
    observed_by_model: ClassVar[bool] = False

    sql: str
    params: list[Any] | None
    duration_ms: float
    error: Exception | None
    connection_name: str
