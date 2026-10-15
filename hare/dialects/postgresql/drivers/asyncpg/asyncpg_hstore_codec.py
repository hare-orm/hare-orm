from __future__ import annotations

import struct
from collections.abc import Callable, Mapping
from typing import Any, ClassVar

from hare.dialects.postgresql.drivers.asyncpg.constants import HSTORE_NULL_LENGTH
from hare.dialects.postgresql.fields.hstore.h_store_text import HStoreText
from hare.native.native_modules import NativeModules


class AsyncpgHstoreCodec:
    """The binary ``hstore`` codec of an asyncpg connection, taking and giving the text PostgreSQL
    reads and writes (a mapping is taken too). asyncpg reads ``hstore`` as text otherwise, and its
    text array parser crashes on an empty ``hstore`` in an array."""

    #: The compiled decoder - None without the compiled extension.
    native_decoder: ClassVar[Any] = getattr(NativeModules.rows, "read_hstore_text", None)

    @staticmethod
    def encode(value: Any) -> bytes:
        """Encodes an hstore text or a mapping as PostgreSQL's binary ``hstore``. A repeated key is
        left to the server, which keeps one as it does for text.

        Args:
            value: The text, or a mapping of strings to strings or None.

        Returns:
            The binary value.

        Raises:
            TypeError: The value is neither, or a key or value isn't a string.
            ValueError: The text isn't an hstore value.
        """
        if isinstance(value, str):
            pairs: list[tuple[Any, Any]] = list(HStoreText.parse_pairs(value))
        elif isinstance(value, Mapping):
            pairs = list(value.items())
        else:
            raise TypeError(f"expected str or a mapping, got {type(value).__name__}")
        parts = [struct.pack("!i", len(pairs))]
        for key, item in pairs:
            if not isinstance(key, str) or not (item is None or isinstance(item, str)):
                raise TypeError("an hstore key is a str and its value a str or None")
            key_bytes = key.encode()
            parts.append(struct.pack("!i", len(key_bytes)) + key_bytes)
            if item is None:
                parts.append(struct.pack("!i", HSTORE_NULL_LENGTH))
            else:
                item_bytes = item.encode()
                parts.append(struct.pack("!i", len(item_bytes)) + item_bytes)
        return b"".join(parts)

    @classmethod
    def get_decoder(cls) -> Callable[[bytes], str]:
        """The decoder of the binary value - the compiled one, ``decode()`` without it."""
        return cls.native_decoder or cls.decode

    @staticmethod
    def decode(value: bytes) -> str:
        """Decodes PostgreSQL's binary ``hstore`` into its text, as ``hstore_out`` writes it.

        Args:
            value: The binary value.

        Returns:
            The text.
        """
        count = int.from_bytes(value[:4], "big")
        offset = 4
        parts = []
        for __ in range(count):
            key_length = int.from_bytes(value[offset : offset + 4], "big")
            offset += 4
            key = HStoreText.quote(value[offset : offset + key_length].decode())
            offset += key_length
            item_length = int.from_bytes(value[offset : offset + 4], "big", signed=True)
            offset += 4
            if item_length == HSTORE_NULL_LENGTH:
                parts.append(f"{key}=>NULL")
                continue
            parts.append(f"{key}=>{HStoreText.quote(value[offset : offset + item_length].decode())}")
            offset += item_length
        return ", ".join(parts)
