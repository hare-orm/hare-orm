from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

from hare.core.connections.messages import CONNECTION_RELEASE_FAILED
from hare.core.resource_completion import ResourceCompletion
from hare.dialects.clickhouse.keeper.constants import KEEPER_SESSION_TIMEOUT
from hare.exceptions import ConfigurationError, DBConnectionError, OperationalError

if TYPE_CHECKING:
    from kazoo.client import KazooClient

TResult = TypeVar("TResult")


class KeeperSession:
    """An asyncio boundary around Kazoo's session and fixed-node operations.

    Blocking library calls retain their worker until completion, even during cancellation.
    After a connected session becomes suspended or lost, this adapter is terminal: it
    stops the client rather than accepting Kazoo's reconnect as restored lock ownership.
    """

    def __init__(self, addresses: tuple[tuple[str, int], ...], connect_timeout: float) -> None:
        """Store Keeper addresses and the connection establishment timeout in seconds."""
        self.addresses = addresses
        self.connect_timeout = connect_timeout
        self._client: KazooClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._connected = False
        self._workers: set[asyncio.Task[Any]] = set()
        self._deletions: set[asyncio.Event] = set()
        self._close_task: asyncio.Task[None] | None = None
        self.end_error: DBConnectionError | None = None
        #: Installed only while a migration's critical scope is active, on the event loop.
        self.on_end: Callable[[], None] | None = None

    async def open(self) -> None:
        """Open a session; a failed or cancelled start releases all library resources.

        Raises:
            ConfigurationError: The optional ``clickhouse-keeper`` extra is missing.
            DBConnectionError: No Keeper server accepted the session.
        """
        try:
            from kazoo.client import KazooClient
        except ImportError as error:
            raise ConfigurationError("ClickHouse Keeper requires pip install hare-orm[clickhouse-keeper]") from error

        self._loop = asyncio.get_running_loop()
        self._client = KazooClient(
            hosts=",".join(f"{host}:{port}" for host, port in self.addresses),
            timeout=KEEPER_SESSION_TIMEOUT,
            randomize_hosts=False,
        )
        self._client.add_listener(self.state_changed)
        try:
            # Start owns its handshake timeout. Do not race stop against start_async's setup.
            await ResourceCompletion.wait(asyncio.to_thread(self._client.start, self.connect_timeout))
            self.check()
        except BaseException as error:
            try:
                await self.close()
            except BaseException as release_error:
                if release_error is error:
                    raise
                raise BaseExceptionGroup(CONNECTION_RELEASE_FAILED, [error, release_error]) from None
            if isinstance(error, Exception):
                raise DBConnectionError(f"No ClickHouse Keeper server opened a session: {error!r}") from error
            raise

    def state_changed(self, state: str) -> None:
        """Dispatch a Kazoo thread's state event to the owning asyncio loop."""
        assert self._loop is not None  # nosec B101 - listener registered by open()
        self._loop.call_soon_threadsafe(self.receive_state, state)

    def receive_state(self, state: str) -> None:
        """Remember the first connection; all later uncertainty permanently ends ownership."""
        from kazoo.protocol.states import KazooState

        if state == KazooState.CONNECTED:
            self._connected = True
        elif self._connected:
            self.end(DBConnectionError(f"The ClickHouse Keeper session is {state.lower()}"))

    def check(self) -> None:
        """Reject work after the session has ended, including after an automatic reconnect."""
        if self.end_error is not None:
            raise self.end_error

    def end(self, error: DBConnectionError) -> None:
        """Wake waiters, interrupt the live migration scope, and schedule one terminal close."""
        if self.end_error is not None:
            return
        self.end_error = error
        for deletion in self._deletions:
            deletion.set()
        if self.on_end is not None:
            self.on_end()
        self.start_close()

    async def run(self, operation: Callable[..., TResult], *args: Any, **kwargs: Any) -> TResult:
        """Drain a library call; preserve node outcomes and translate other Keeper failures."""
        from kazoo.exceptions import (
            AuthFailedError,
            ConnectionLoss,
            KazooException,
            NodeExistsError,
            NoNodeError,
            NotEmptyError,
            SessionExpiredError,
            ZookeeperStoppedError,
        )

        self.check()
        worker = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
        self._workers.add(worker)
        try:
            result = await ResourceCompletion.wait(
                worker, on_cancel=lambda error: self.end(DBConnectionError("The Keeper operation was cancelled"))
            )
            self.check()
            return result
        except (NoNodeError, NodeExistsError, NotEmptyError):
            self.check()
            raise
        except (AuthFailedError, ConnectionLoss, SessionExpiredError, ZookeeperStoppedError, OSError) as error:
            failure = DBConnectionError(f"The ClickHouse Keeper session was lost: {error!r}")
            self.end(failure)
            raise self.end_error or failure from error
        except KazooException as error:
            self.check()
            raise OperationalError(f"ClickHouse Keeper refused the request: {error!r}") from error
        finally:
            self._workers.discard(worker)

    async def create(self, path: str) -> bool:
        """Create the fixed ephemeral lock and missing persistent parents; False if already held."""
        from kazoo.exceptions import NodeExistsError

        assert self._client is not None  # nosec B101 - operations follow open()
        try:
            await self.run(self._client.create, path, ephemeral=True, makepath=True)
        except NodeExistsError:
            return False
        return True

    async def delete(self, path: str) -> None:
        """Delete an empty node; leave absent nodes and nodes still holding children alone."""
        from kazoo.exceptions import NoNodeError, NotEmptyError

        assert self._client is not None  # nosec B101 - operations follow open()
        try:
            await self.run(self._client.delete, path)
        except (NoNodeError, NotEmptyError):
            pass

    async def get_children(self, path: str) -> list[str]:
        """Return child names, or an empty list when the parent does not exist."""
        from kazoo.exceptions import NoNodeError

        assert self._client is not None  # nosec B101 - operations follow open()
        try:
            return await self.run(self._client.get_children, path)
        except NoNodeError:
            return []

    async def wait_for_deletion(self, path: str) -> None:
        """Wait for a node change, or return immediately if it already disappeared."""
        assert self._client is not None and self._loop is not None  # nosec B101 - operations follow open()
        loop = self._loop
        deletion = asyncio.Event()
        self._deletions.add(deletion)
        try:
            exists = await self.run(
                self._client.exists,
                path,
                watch=lambda event: loop.call_soon_threadsafe(deletion.set),
            )
            if exists is not None:
                await deletion.wait()
            self.check()
        finally:
            self._deletions.discard(deletion)

    def start_close(self) -> asyncio.Task[None]:
        """Keep one cleanup task so concurrent closes and repeated cancellation share it."""
        if self._close_task is None:
            self._close_task = asyncio.create_task(self.close_session())
            # Loss can close an idle session before its owner next awaits close(). Retrieve
            # the result now to avoid an orphaned failure; close() still re-raises it.
            self._close_task.add_done_callback(lambda task: None if task.cancelled() else task.exception())
        return self._close_task

    async def close(self) -> None:
        """Stop the library, close its handles and drain pending calls before returning."""
        await ResourceCompletion.wait(self.start_close())

    async def close_session(self) -> None:
        """Run terminal cleanup once; callbacks and operations cannot revive this session."""
        self.end(DBConnectionError("The ClickHouse Keeper session is closed"))
        try:
            if self._client is not None:
                await asyncio.to_thread(self.stop_client)
        finally:
            if self._workers:
                await asyncio.gather(*self._workers, return_exceptions=True)

    def stop_client(self) -> None:
        """Remove callbacks, stop the session and release handles even if stopping fails."""
        assert self._client is not None  # nosec B101 - checked by close_session()
        self._client.remove_listener(self.state_changed)
        errors = []
        for operation in (self._client.stop, self._client.close):
            try:
                operation()
            except BaseException as error:
                errors.append(error)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup(CONNECTION_RELEASE_FAILED, errors)
