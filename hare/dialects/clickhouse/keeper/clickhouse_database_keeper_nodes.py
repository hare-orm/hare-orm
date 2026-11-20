from __future__ import annotations

import urllib.parse

from hare.core.connections.messages import CONNECTION_RELEASE_FAILED
from hare.dialects.clickhouse.keeper.clickhouse_migration_lock import ClickhouseMigrationLock
from hare.dialects.clickhouse.keeper.keeper_session import KeeperSession


class ClickhouseDatabaseKeeperNodes:
    """The nodes ClickHouse Keeper keeps for a database - the series of its generated keys
    (``database.app.Model``) and the node its migration lock is taken under - deleted with the database: a
    server keeps at most ``max_autoincrement_series`` series, a test database of its own each test run
    adding its series."""

    def __init__(self, addresses: tuple[tuple[str, int], ...], database: str, connect_timeout: float) -> None:
        """
        Args:
            addresses: The hosts and ports of the Keeper servers.
            database: The database deleted.
            connect_timeout: Seconds allowed to establish a Keeper session.
        """
        self.addresses = addresses
        self.database = database
        self.migration_locks_path = ClickhouseMigrationLock.get_database_path(database)
        self.connect_timeout = connect_timeout

    def is_series_of_database(self, node_name: str) -> bool:
        """Whether a node of the server's series is one of the database's - the server names the node after
        the series, each byte but a letter, a digit and ``_`` written as ``%XX``. A series is named
        ``<database>.<app_label>.<Model>``, an app label and a model name holding no dot - the database of a
        series is what comes before its last two dots, which tells database ``a`` from database ``a.b``.

        Args:
            node_name: The node's name.

        Returns:
            True for a series of the database's models.
        """
        series_name_parts = urllib.parse.unquote(node_name).rsplit(".", 2)
        return len(series_name_parts) == 3 and series_name_parts[0] == self.database

    async def delete(self, series_keeper_path: str) -> None:
        """Deletes the database's series and the node of its migration lock - left while another ``migrate``
        holds the lock.

        Args:
            series_keeper_path: The node the server keeps its series under; empty for a server keeping none.

        Raises:
            DBConnectionError: No Keeper server opened a session.
            OperationalError: Keeper refused a deletion.
        """
        session = KeeperSession(self.addresses, self.connect_timeout)
        await session.open()
        try:
            if series_keeper_path:
                for node_name in await session.get_children(series_keeper_path):
                    if self.is_series_of_database(node_name):
                        await session.delete(f"{series_keeper_path}/{node_name}")
            await session.delete(self.migration_locks_path)
        except BaseException as error:
            try:
                await session.close()
            except BaseException as release_error:
                if release_error is error:
                    raise
                raise BaseExceptionGroup(CONNECTION_RELEASE_FAILED, [error, release_error]) from None
            raise
        else:
            await session.close()
