from __future__ import annotations

import asyncio
from collections.abc import Sequence

from hare.dialects.base.client.declarations import RowLockOutcome
from hare.dialects.clickhouse.keeper.constants import (
    KEEPER_DEFAULT_PORT,
    KEEPER_EPHEMERAL_NODE,
    KEEPER_NO_ERROR,
    KEEPER_NO_NODE_ERROR,
    KEEPER_NODE_EXISTS_ERROR,
    KEEPER_PERSISTENT_NODE,
    KEEPER_ROW_LOCK_LIMIT,
    KEEPER_ROW_LOCKS_PATH,
)
from hare.dialects.clickhouse.keeper.keeper_session import KeeperSession
from hare.exceptions import ConfigurationError, OperationalError, QueryError


class ClickhouseRowLocks:
    """The row locks of a ClickHouse transaction: an ephemeral node of ClickHouse Keeper per row,
    ``/hare/locks/<database>/<table>/<key>``, in a session the transaction opens with its first lock and
    closes as it ends - the server deletes the nodes with the session. The locks are taken in the order
    of their names, so two transactions never wait for each other's next lock."""

    def __init__(
        self,
        addresses: tuple[tuple[str, int], ...],
        database: str,
        connect_timeout: float,
        lock_timeout: float | None,
    ) -> None:
        """
        Args:
            addresses: The hosts and ports of the Keeper servers.
            database: The database whose rows are locked.
            connect_timeout: Seconds a server may take to open the session.
            lock_timeout: Seconds to wait for the locks of a call - None to wait as long as they are held.
        """
        self.addresses = addresses
        self.root_path = f"{KEEPER_ROW_LOCKS_PATH}/{database}"
        self.connect_timeout = connect_timeout
        self.lock_timeout = lock_timeout
        self.session: KeeperSession | None = None
        #: The names of the locks held.
        self.held: set[str] = set()

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
            if not host or not 1 <= port <= 65535:
                raise ConfigurationError(
                    f"keeper_hosts={keeper_hosts!r} is not a list of host:port addresses separated by commas"
                )
            addresses.append((host, port))
        return tuple(addresses)

    @property
    def is_lost(self) -> bool:
        """Whether the session holding the locks ended before the transaction did."""
        return self.session is not None and self.session.end_error is not None

    async def check(self) -> None:
        """Checks that a Keeper server opens a session.

        Raises:
            DBConnectionError: None did.
        """
        session = KeeperSession(self.addresses, self.connect_timeout)
        await session.open()
        await session.close()

    async def take(self, lock_names: Sequence[str], *, wait: bool) -> RowLockOutcome:
        """Takes locks by their names.

        Args:
            lock_names: The names - ``<table>/<key>`` each.
            wait: Whether to wait for the locks other transactions hold.

        Returns:
            What was taken.

        Raises:
            QueryError: The transaction would hold more locks than ``KEEPER_ROW_LOCK_LIMIT``.
            OperationalError: Keeper refused a lock, or the wait outlasted the lock timeout.
            DBConnectionError: The session was lost.
        """
        requested = set(lock_names)
        names = sorted(requested - self.held)
        if len(self.held) + len(names) > KEEPER_ROW_LOCK_LIMIT:
            raise QueryError(
                f"select_for_update() would hold {len(self.held) + len(names)} row locks in one transaction - "
                f"ClickHouse Keeper keeps at most {KEEPER_ROW_LOCK_LIMIT}; lock fewer rows at a time"
            )
        session = await self.get_session()
        loop = asyncio.get_running_loop()
        deadline = None if self.lock_timeout is None else loop.time() + self.lock_timeout
        busy: list[str] = []
        waited: dict[str, None] = {}
        position = 0
        while position < len(names):
            pending = names[position:]
            error_codes = await asyncio.gather(*[self.create(session, name) for name in pending])
            for index, (name, error_code) in enumerate(zip(pending, error_codes, strict=True)):
                if error_code == KEEPER_NO_ERROR:
                    self.held.add(name)
                    continue
                if not wait:
                    busy.append(name)
                    continue
                # The locks taken past it are given back, so the locks are held in the order of their names.
                taken_later = [
                    later_name
                    for later_name, later_code in zip(pending[index + 1 :], error_codes[index + 1 :], strict=True)
                    if later_code == KEEPER_NO_ERROR
                ]
                await asyncio.gather(*[session.delete(self.get_path(later_name)) for later_name in taken_later])
                self.held.difference_update(taken_later)
                await self.wait_for(session, name, deadline)
                waited[name] = None
                position += index
                break
            else:
                position = len(names)
        return RowLockOutcome(taken=tuple(sorted(requested.difference(busy))), busy=tuple(busy), waited=tuple(waited))

    async def get_session(self) -> KeeperSession:
        """The session holding the locks, opened with the first lock.

        Returns:
            The session.
        """
        if self.session is None:
            session = KeeperSession(self.addresses, self.connect_timeout)
            await session.open()
            self.session = session
        return self.session

    def get_path(self, name: str) -> str:
        """The node of a lock.

        Args:
            name: The lock's name.

        Returns:
            The node's path.
        """
        return f"{self.root_path}/{name}"

    async def create(self, session: KeeperSession, name: str) -> int:
        """Creates the node of a lock - with the nodes above it, when missing.

        Args:
            session: The session.
            name: The lock's name.

        Returns:
            The error code: none, or the node held by another session.

        Raises:
            OperationalError: Keeper refused the node.
        """
        path = self.get_path(name)
        error_code = await session.create(path, KEEPER_EPHEMERAL_NODE)
        if error_code == KEEPER_NO_NODE_ERROR:
            parent_path = ""
            for part in path.split("/")[1:-1]:
                parent_path = f"{parent_path}/{part}"
                parent_error_code = await session.create(parent_path, KEEPER_PERSISTENT_NODE)
                if parent_error_code not in {KEEPER_NO_ERROR, KEEPER_NODE_EXISTS_ERROR}:
                    raise OperationalError(
                        f"ClickHouse Keeper refused the node {parent_path!r}: error {parent_error_code}"
                    )
            error_code = await session.create(path, KEEPER_EPHEMERAL_NODE)
        if error_code not in {KEEPER_NO_ERROR, KEEPER_NODE_EXISTS_ERROR}:
            raise OperationalError(f"ClickHouse Keeper refused the row lock {path!r}: error {error_code}")
        return error_code

    async def wait_for(self, session: KeeperSession, name: str, deadline: float | None) -> None:
        """Waits until another transaction gives a lock back.

        Args:
            session: The session.
            name: The lock's name.
            deadline: The loop time the wait ends at - None for no end.

        Raises:
            OperationalError: The deadline passed.
        """
        remaining = None if deadline is None else deadline - asyncio.get_running_loop().time()
        try:
            if remaining is not None and remaining <= 0:
                raise TimeoutError
            await asyncio.wait_for(session.wait_for_deletion(self.get_path(name)), remaining)
        except TimeoutError:
            raise OperationalError(
                f"select_for_update() waited longer than the transaction's lock_timeout ({self.lock_timeout}s) for "
                f"the row lock {name!r}"
            ) from None

    async def close(self) -> None:
        """Gives every lock back, closing the session."""
        session, self.session = self.session, None
        self.held.clear()
        if session is not None:
            await session.close()
