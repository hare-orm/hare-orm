from __future__ import annotations

import asyncio
import contextlib
import struct

from hare.dialects.clickhouse.keeper.constants import (
    KEEPER_CLOSE_SESSION_OPERATION,
    KEEPER_CREATE_OPERATION,
    KEEPER_DELETE_OPERATION,
    KEEPER_EXISTS_OPERATION,
    KEEPER_NO_NODE_ERROR,
    KEEPER_PING_OPERATION,
    KEEPER_PING_REQUEST_ID,
    KEEPER_PINGS_PER_SESSION_TIMEOUT,
    KEEPER_SESSION_TIMEOUT_MILLISECONDS,
    KEEPER_WATCH_EVENT_REQUEST_ID,
)
from hare.dialects.clickhouse.keeper.keeper_codec import KeeperCodec
from hare.exceptions import DBConnectionError


class KeeperSession:
    """A session of ClickHouse Keeper on a connection of its own: each request answered by its id - several
    can be on their way at once - the deletion of a node watched, pings keeping the session. The server
    deletes the ephemeral nodes of the session as it closes or is lost."""

    def __init__(self, addresses: tuple[tuple[str, int], ...], connect_timeout: float) -> None:
        """
        Args:
            addresses: The hosts and ports of the Keeper servers - the first taking the session holds it.
            connect_timeout: Seconds a server may take to open the session.
        """
        self.addresses = addresses
        self.connect_timeout = connect_timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        #: The replies awaited, by request id.
        self._replies: dict[int, asyncio.Future[tuple[int, bytes]]] = {}
        #: The deletions awaited, by node path.
        self._deletions: dict[str, asyncio.Future[None]] = {}
        self._next_request_id = 1
        self._tasks: list[asyncio.Task[None]] = []
        #: Why the session ended - None while it holds.
        self.end_error: Exception | None = None

    async def open(self) -> None:
        """Opens the session on the first server taking it.

        Raises:
            DBConnectionError: No server took it.
        """
        failures: list[str] = []
        for host, port in self.addresses:
            writer: asyncio.StreamWriter | None = None
            try:
                reader, opened_writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port), self.connect_timeout
                )
                writer = opened_writer
                opened_writer.write(KeeperCodec.get_connect_request(KEEPER_SESSION_TIMEOUT_MILLISECONDS))
                await opened_writer.drain()
                payload = await asyncio.wait_for(self.read_frame(reader), self.connect_timeout)
            except (OSError, TimeoutError, asyncio.IncompleteReadError) as error:
                failures.append(f"{host}:{port}: {error!r}")
                if writer is not None:
                    writer.close()
                continue
            session_timeout_milliseconds, _session_id = KeeperCodec.read_connect_reply(payload)
            if session_timeout_milliseconds <= 0:
                failures.append(f"{host}:{port}: the session was refused")
                opened_writer.close()
                continue
            self._reader, self._writer = reader, opened_writer
            ping_interval = session_timeout_milliseconds / 1000 / KEEPER_PINGS_PER_SESSION_TIMEOUT
            self._tasks = [asyncio.create_task(self.read_replies()), asyncio.create_task(self.ping(ping_interval))]
            return
        raise DBConnectionError(f"No ClickHouse Keeper server opened a session ({'; '.join(failures)})")

    @staticmethod
    async def read_frame(reader: asyncio.StreamReader) -> bytes:
        """Reads a frame of the connection.

        Args:
            reader: The connection.

        Returns:
            The frame's payload.
        """
        (size,) = struct.unpack("!i", await reader.readexactly(4))
        return await reader.readexactly(size)

    async def read_replies(self) -> None:
        """Hands each reply to the request it answers, and each watch event to the deletion awaited."""
        reader = self._reader
        assert reader is not None  # nosec B101 - set by open()
        try:
            while True:
                payload = await self.read_frame(reader)
                request_id, error_code = KeeperCodec.read_reply_header(payload)
                if request_id == KEEPER_WATCH_EVENT_REQUEST_ID:
                    # Any change of a watched node ends the wait - the waiter looks again.
                    _event_type, path = KeeperCodec.read_watch_event(payload)
                    deletion = self._deletions.pop(path, None)
                    if deletion is not None and not deletion.done():
                        deletion.set_result(None)
                    continue
                if request_id == KEEPER_PING_REQUEST_ID:
                    continue
                reply = self._replies.pop(request_id, None)
                if reply is not None and not reply.done():
                    reply.set_result((error_code, payload[16:]))
        except (OSError, asyncio.IncompleteReadError) as error:
            self.end(DBConnectionError(f"The ClickHouse Keeper session was lost: {error!r}"))

    async def ping(self, interval: float) -> None:
        """Pings the server so it keeps the session.

        Args:
            interval: Seconds between pings.
        """
        writer = self._writer
        assert writer is not None  # nosec B101 - set by open()
        try:
            while True:
                await asyncio.sleep(interval)
                writer.write(KeeperCodec.get_request(KEEPER_PING_REQUEST_ID, KEEPER_PING_OPERATION, b""))
                await writer.drain()
        except OSError as error:
            self.end(DBConnectionError(f"The ClickHouse Keeper session was lost: {error!r}"))

    def end(self, error: Exception) -> None:
        """Ends the session: every request and wait still open fails with the error.

        Args:
            error: Why the session ended.
        """
        if self.end_error is not None:
            return
        self.end_error = error
        for reply in self._replies.values():
            if not reply.done():
                reply.set_exception(error)
        for deletion in self._deletions.values():
            if not deletion.done():
                deletion.set_exception(error)
        self._replies.clear()
        self._deletions.clear()
        if self._writer is not None:
            self._writer.close()

    async def send(self, operation: int, body: bytes) -> tuple[int, bytes]:
        """Sends a request and awaits its reply.

        Args:
            operation: The operation.
            body: The operation's arguments.

        Returns:
            The error code and the rest of the reply.

        Raises:
            DBConnectionError: The session ended.
        """
        if self.end_error is not None:
            raise self.end_error
        writer = self._writer
        assert writer is not None  # nosec B101 - set by open()
        request_id = self._next_request_id
        self._next_request_id += 1
        reply: asyncio.Future[tuple[int, bytes]] = asyncio.get_running_loop().create_future()
        self._replies[request_id] = reply
        writer.write(KeeperCodec.get_request(request_id, operation, body))
        await writer.drain()
        return await reply

    async def create(self, path: str, flags: int) -> int:
        """Creates an empty node.

        Args:
            path: The node's path.
            flags: Whether the node is ephemeral.

        Returns:
            The error code.
        """
        error_code, _reply = await self.send(KEEPER_CREATE_OPERATION, KeeperCodec.get_create_body(path, flags))
        return error_code

    async def delete(self, path: str) -> int:
        """Deletes a node.

        Args:
            path: The node's path.

        Returns:
            The error code.
        """
        error_code, _reply = await self.send(KEEPER_DELETE_OPERATION, KeeperCodec.get_delete_body(path))
        return error_code

    async def wait_for_deletion(self, path: str) -> None:
        """Waits until a node is deleted or changed - at once when there is none.

        Args:
            path: The node's path.
        """
        deletion = self._deletions.get(path)
        if deletion is None or deletion.done():
            deletion = self._deletions[path] = asyncio.get_running_loop().create_future()
        error_code, _reply = await self.send(KEEPER_EXISTS_OPERATION, KeeperCodec.get_exists_body(path))
        if error_code == KEEPER_NO_NODE_ERROR:
            self._deletions.pop(path, None)
            return
        await deletion

    async def close(self) -> None:
        """Closes the session - the server deletes its ephemeral nodes."""
        if self.end_error is None:
            with contextlib.suppress(DBConnectionError, OSError, TimeoutError):
                await asyncio.wait_for(self.send(KEEPER_CLOSE_SESSION_OPERATION, b""), self.connect_timeout)
            self.end(DBConnectionError("The ClickHouse Keeper session is closed"))
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
