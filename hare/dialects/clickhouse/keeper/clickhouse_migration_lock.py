from __future__ import annotations

import asyncio
import urllib.parse
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from hare.core.connections.messages import CONNECTION_RELEASE_FAILED
from hare.dialects.clickhouse.keeper.constants import (
    KEEPER_DEFAULT_PORT,
    KEEPER_MIGRATION_LOCK_NAME,
    KEEPER_MIGRATION_LOCKS_PATH,
)
from hare.dialects.clickhouse.keeper.keeper_session import KeeperSession
from hare.exceptions import ConfigurationError


class ClickhouseMigrationLock:
    """The lock ``migrate`` holds for its whole run: one ephemeral node of ClickHouse Keeper per
    database, ``/hare/migrations/<database>/migrate`` - a second ``migrate`` waits until the first
    one's session ends, the server deleting the node with it, also when the process dies. The database's
    name is written escaped (``get_database_path()``), so no name reaches into another database's node."""

    def __init__(self, addresses: tuple[tuple[str, int], ...], database: str, connect_timeout: float) -> None:
        """
        Args:
            addresses: The hosts and ports of the Keeper servers.
            database: The database migrated.
            connect_timeout: Seconds allowed to establish a Keeper session.
        """
        self.addresses = addresses
        self.path = f"{ClickhouseMigrationLock.get_database_path(database)}/{KEEPER_MIGRATION_LOCK_NAME}"
        self.connect_timeout = connect_timeout
        self.session: KeeperSession | None = None

    @staticmethod
    def get_database_path(database: str) -> str:
        """The node a database's migration lock is taken under - its name with each byte but a letter, a
        digit and ``_.-~`` written as ``%XX``: a ``/`` of the name would put the node under another
        database's, ``a/migrate`` under the lock of ``a``.

        Args:
            database: The database.

        Returns:
            ``/hare/migrations/<escaped database>``.
        """
        return f"{KEEPER_MIGRATION_LOCKS_PATH}/{urllib.parse.quote(database, safe='')}"

    @staticmethod
    def get_addresses(keeper_hosts: str) -> tuple[tuple[str, int], ...]:
        """Reads the ``keeper_hosts`` setting of a connection: ``host[:port]`` separated by commas.

        Args:
            keeper_hosts: The setting.

        Returns:
            The host and port of each server.

        Raises:
            ConfigurationError: An address is not a host with a port of 1-65535.
        """
        addresses: list[tuple[str, int]] = []
        for address in keeper_hosts.split(","):
            host, separator, port_text = address.strip().rpartition(":")
            if not separator:
                host, port_text = port_text, ""
            port = KEEPER_DEFAULT_PORT if not port_text else int(port_text) if port_text.isdigit() else 0
            if not host or "\x00" in host or not 1 <= port <= 65535:
                raise ConfigurationError(
                    f"keeper_hosts={keeper_hosts!r} is not a list of host:port addresses separated by commas"
                )
            addresses.append((host, port))
        return tuple(addresses)

    async def check(self) -> None:
        """Checks that a Keeper server opens a session.

        Raises:
            DBConnectionError: None did.
        """
        session = KeeperSession(self.addresses, self.connect_timeout)
        await session.open()
        await session.close()

    async def take(self) -> None:
        """Takes the lock, waiting while another session holds it.

        Raises:
            OperationalError: Keeper refused the node.
            DBConnectionError: The session was lost.
        """
        session = KeeperSession(self.addresses, self.connect_timeout)
        await session.open()
        self.session = session
        while not await session.create(self.path):
            await session.wait_for_deletion(self.path)

    @asynccontextmanager
    async def hold(self) -> AsyncGenerator[None]:
        """Hold the fixed lock and interrupt the critical scope if its session becomes uncertain.

        A lost or suspended session is terminal, even if Kazoo reconnects. The standard
        timeout context owns the interruption; unrelated timeouts and external cancellation
        keep their identity. Cleanup completes before the scope returns.
        """
        try:
            await self.take()
            session = self.session
            assert session is not None  # nosec B101 - set by take()
            loop = asyncio.get_running_loop()
            scope = asyncio.timeout(None)
            try:
                async with scope:
                    session.check()
                    session.on_end = lambda: scope.reschedule(loop.time())
                    try:
                        yield
                        session.check()
                    finally:
                        session.on_end = None
            except TimeoutError:
                if scope.expired():
                    session.check()
                raise
        except BaseException as error:
            try:
                await self.close()
            except BaseException as release_error:
                if release_error is error:
                    raise
                raise BaseExceptionGroup(CONNECTION_RELEASE_FAILED, [error, release_error]) from None
            raise
        else:
            await self.close()

    async def close(self) -> None:
        """Gives the lock back, closing the session."""
        session, self.session = self.session, None
        if session is not None:
            await session.close()
