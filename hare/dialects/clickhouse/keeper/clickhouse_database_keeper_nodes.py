from __future__ import annotations

import urllib.parse

from hare.dialects.clickhouse.keeper.clickhouse_migration_lock import ClickhouseMigrationLock
from hare.dialects.clickhouse.keeper.constants import (
    KEEPER_NO_ERROR,
    KEEPER_NO_NODE_ERROR,
    KEEPER_NOT_EMPTY_ERROR,
)
from hare.dialects.clickhouse.keeper.keeper_session import KeeperSession
from hare.exceptions import OperationalError


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
            connect_timeout: Seconds a server may take to open the session.
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
                        await self.delete_node(session, f"{series_keeper_path}/{node_name}")
            await self.delete_node(session, self.migration_locks_path)
        finally:
            await session.close()

    @staticmethod
    async def delete_node(session: KeeperSession, path: str) -> None:
        """Deletes a node - one already gone, or with children, is left.

        Args:
            session: The session.
            path: The node's path.

        Raises:
            OperationalError: Keeper refused the deletion.
        """
        error_code = await session.delete(path)
        if error_code not in {KEEPER_NO_ERROR, KEEPER_NO_NODE_ERROR, KEEPER_NOT_EMPTY_ERROR}:
            raise OperationalError(f"ClickHouse Keeper refused to delete {path!r}: error {error_code}")
