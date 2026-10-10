from __future__ import annotations

from typing import Any

from orm_benchmark.constants import POSTGRESQL_PORT


class PostgresqlDatabase:
    """The throwaway database a run creates on the PostgreSQL server and drops afterwards."""

    def __init__(self, name: str, port: int = POSTGRESQL_PORT) -> None:
        """
        Args:
            name: The database's name.
            port: The port of the server at 127.0.0.1.
        """
        self.name = name
        self.port = port

    async def connect(self) -> Any:
        """A connection to the server's ``postgres`` database."""
        import asyncpg

        return await asyncpg.connect(
            host="127.0.0.1", port=self.port, user="postgres", password="postgres", database="postgres"
        )

    async def create(self) -> str:
        """Creates the database afresh.

        Returns:
            The server's version.
        """
        connection = await self.connect()
        try:
            await connection.execute(f'DROP DATABASE IF EXISTS "{self.name}"')
            await connection.execute(f'CREATE DATABASE "{self.name}"')
            return str(await connection.fetchval("SHOW server_version"))
        finally:
            await connection.close()

    async def drop(self) -> None:
        """Drops the database - a failure here costs nothing but a leftover database, which the next
        run's create() drops."""
        try:
            connection = await self.connect()
            try:
                await connection.execute(f'DROP DATABASE IF EXISTS "{self.name}"')
            finally:
                await connection.close()
        except Exception as error:  # noqa: BLE001 - never lose a finished run over the clean-up
            print(f"(the database {self.name} stays: {error})")
