from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect


@dataclasses.dataclass(frozen=True, slots=True)
class QueryCall:
    """A query-executing call a ``QueryWrapper`` runs around.

    Attributes:
        method_name: The client method - ``execute``, ``execute_many``, ``execute_script``,
            ``execute_described``, ``copy`` or ``stream``.
        sql: The statement text; ``COPY <table> (<columns>) FROM STDIN`` for a bulk load.
        params: The bound parameters, None when there are none to show.
        connection_name: The connection it runs on.
        dialect: The connection's dialect.
    """

    method_name: str
    sql: str
    params: list[Any] | None
    connection_name: str
    dialect: Dialect
