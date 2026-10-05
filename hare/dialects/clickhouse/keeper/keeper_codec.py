from __future__ import annotations

import struct

from hare.dialects.clickhouse.keeper.constants import (
    KEEPER_ANY_VERSION,
    KEEPER_NEW_SESSION_PASSWORD,
    KEEPER_OPEN_ACL_ID,
    KEEPER_OPEN_ACL_PERMISSIONS,
    KEEPER_OPEN_ACL_SCHEME,
    KEEPER_PROTOCOL_VERSION,
)


class KeeperCodec:
    """Writes the requests and reads the replies of the ZooKeeper protocol - lengths and numbers are
    big-endian."""

    @staticmethod
    def get_string(value: str) -> bytes:
        """A string: its length, then its UTF-8 bytes.

        Args:
            value: The string.

        Returns:
            The bytes.
        """
        data = value.encode()
        return struct.pack("!i", len(data)) + data

    @staticmethod
    def get_buffer(data: bytes) -> bytes:
        """A buffer: its length, then its bytes.

        Args:
            data: The bytes.

        Returns:
            The buffer.
        """
        return struct.pack("!i", len(data)) + data

    @staticmethod
    def get_frame(payload: bytes) -> bytes:
        """A frame of the connection: the length of the payload, then the payload.

        Args:
            payload: The payload.

        Returns:
            The frame.
        """
        return struct.pack("!i", len(payload)) + payload

    @staticmethod
    def get_request(request_id: int, operation: int, body: bytes) -> bytes:
        """The frame of a request.

        Args:
            request_id: The id the reply answers with.
            operation: The operation.
            body: The operation's arguments.

        Returns:
            The frame.
        """
        return KeeperCodec.get_frame(struct.pack("!ii", request_id, operation) + body)

    @staticmethod
    def get_connect_request(session_timeout_milliseconds: int) -> bytes:
        """The frame opening a new session.

        Args:
            session_timeout_milliseconds: The session timeout asked for.

        Returns:
            The frame.
        """
        return KeeperCodec.get_frame(
            struct.pack("!iqiq", KEEPER_PROTOCOL_VERSION, 0, session_timeout_milliseconds, 0)
            + KeeperCodec.get_buffer(KEEPER_NEW_SESSION_PASSWORD)
            + b"\x00"
        )

    @staticmethod
    def read_connect_reply(payload: bytes) -> tuple[int, int]:
        """Reads the reply opening a session.

        Args:
            payload: The reply.

        Returns:
            The session timeout in milliseconds - 0 for a session refused - and the session's id.
        """
        _protocol_version, session_timeout_milliseconds, session_id = struct.unpack("!iiq", payload[:16])
        return session_timeout_milliseconds, session_id

    @staticmethod
    def read_reply_header(payload: bytes) -> tuple[int, int]:
        """Reads the head of a reply.

        Args:
            payload: The reply.

        Returns:
            The id of the request answered and the error code.
        """
        request_id, _transaction_id, error_code = struct.unpack("!iqi", payload[:16])
        return request_id, error_code

    @staticmethod
    def read_watch_event(payload: bytes) -> tuple[int, str]:
        """Reads a watch event after the head of its reply.

        Args:
            payload: The reply.

        Returns:
            The type of the event and the path of its node.
        """
        event_type, _state, path_length = struct.unpack("!iii", payload[16:28])
        return event_type, payload[28 : 28 + path_length].decode()

    @staticmethod
    def get_create_body(path: str, flags: int) -> bytes:
        """The arguments creating an empty node anyone may change.

        Args:
            path: The node's path.
            flags: Whether the node is ephemeral.

        Returns:
            The arguments.
        """
        acl = (
            struct.pack("!ii", 1, KEEPER_OPEN_ACL_PERMISSIONS)
            + KeeperCodec.get_string(KEEPER_OPEN_ACL_SCHEME)
            + KeeperCodec.get_string(KEEPER_OPEN_ACL_ID)
        )
        return KeeperCodec.get_string(path) + KeeperCodec.get_buffer(b"") + acl + struct.pack("!i", flags)

    @staticmethod
    def get_exists_body(path: str) -> bytes:
        """The arguments asking whether a node exists, watching it.

        Args:
            path: The node's path.

        Returns:
            The arguments.
        """
        return KeeperCodec.get_string(path) + b"\x01"

    @staticmethod
    def get_delete_body(path: str) -> bytes:
        """The arguments deleting a node, whatever its version.

        Args:
            path: The node's path.

        Returns:
            The arguments.
        """
        return KeeperCodec.get_string(path) + struct.pack("!i", KEEPER_ANY_VERSION)
