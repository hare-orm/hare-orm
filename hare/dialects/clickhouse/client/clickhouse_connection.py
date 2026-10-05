from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: nocoverage
    from types import TracebackType

    from hare.dialects.clickhouse.client.clickhouse_client import ClickhouseClient


class ClickhouseConnection:
    """What ``acquire_connection()`` of a ClickHouse client gives: the driver's connection for one
    statement, opened on first use. A driver runs its statements concurrently on connections of its own,
    so nothing is locked around them - but around the statements of a transaction."""

    __slots__ = ("client",)

    def __init__(self, client: ClickhouseClient) -> None:
        self.client = client

    async def __aenter__(self) -> Any:
        return await self.client.take_statement_connection()

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        exception_traceback: TracebackType | None,
    ) -> None:
        self.client.give_back_statement_connection(exception)
