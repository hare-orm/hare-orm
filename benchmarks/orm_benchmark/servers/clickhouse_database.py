from __future__ import annotations

import asyncio
import urllib.parse
import urllib.request

from orm_benchmark.constants import CLICKHOUSE_HTTP_PORT, CLICKHOUSE_NATIVE_PORT, CLICKHOUSE_PASSWORD


class ClickhouseDatabase:
    """The throwaway database a run creates on the ClickHouse server and drops afterwards - reached over
    the HTTP interface with the standard library, so a target's own environment needs nothing for it."""

    def __init__(self, name: str) -> None:
        """
        Args:
            name: The database's name.
        """
        self.name = name
        self.http_port = CLICKHOUSE_HTTP_PORT
        self.native_port = CLICKHOUSE_NATIVE_PORT
        self.password = CLICKHOUSE_PASSWORD

    def execute(self, sql: str, database: str = "default") -> str:
        """Runs one statement over HTTP.

        Args:
            sql: The statement.
            database: The database it runs in.

        Returns:
            The response body.
        """
        query = urllib.parse.urlencode({"user": "default", "password": self.password, "database": database})
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.http_port}/?{query}", data=sql.encode("utf-8"), method="POST"
        )
        with urllib.request.urlopen(request, timeout=600) as response:  # noqa: S310 - the local test server
            return response.read().decode("utf-8")

    async def run(self, sql: str) -> str:
        """``execute()`` in the run's database, off the event loop."""
        return await asyncio.to_thread(self.execute, sql, self.name)

    async def create(self) -> str:
        """Creates the database afresh.

        Returns:
            The server's version.
        """
        await asyncio.to_thread(self.execute, f"DROP DATABASE IF EXISTS `{self.name}`")
        await asyncio.to_thread(self.execute, f"CREATE DATABASE `{self.name}`")
        return (await asyncio.to_thread(self.execute, "SELECT version()")).strip()

    async def drop(self) -> None:
        try:
            await asyncio.to_thread(self.execute, f"DROP DATABASE IF EXISTS `{self.name}`")
        except OSError as error:
            print(f"(the database {self.name} stays: {error})")
