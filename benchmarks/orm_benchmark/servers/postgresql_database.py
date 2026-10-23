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

    async def connect(self, database: str = "postgres") -> Any:
        """A connection to one of the server's databases.

        Args:
            database: The database's name - the server's ``postgres`` one by default.
        """
        import asyncpg

        return await asyncpg.connect(
            host="127.0.0.1", port=self.port, user="postgres", password="postgres", database=database
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

    async def settle(self) -> None:
        """Rewrites and analyzes every table of the database, untimed, and keeps autovacuum off them.

        The writes of the scenarios before leave dead rows behind. Whether autovacuum has come by
        before the next scenario is chance: without it the planner still reckons with the bloated
        table and picks a parallel scan whose workers alone cost milliseconds; a plain vacuum leaves
        the file at its bloated size, and every scan still reads all its pages. Every scenario group
        starts from compact tables instead.
        """
        connection = await self.connect(self.name)
        try:
            tables = await connection.fetch(
                "SELECT format('%I.%I', schemaname, tablename) FROM pg_tables "
                "WHERE schemaname NOT IN ('pg_catalog', 'information_schema')"
            )
            names = [row[0] for row in tables]
            for name in names:
                await connection.execute(
                    f"ALTER TABLE {name} SET (autovacuum_enabled = false, toast.autovacuum_enabled = false)"
                )
            if names:
                await connection.execute(f"VACUUM (FULL, ANALYZE) {', '.join(names)}")
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
