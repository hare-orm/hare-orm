from __future__ import annotations

import asyncpg

from hare.dialects.postgresql.drivers.asyncpg.asyncpg_hstore_codec import AsyncpgHstoreCodec
from hare.dialects.postgresql.drivers.asyncpg.asyncpg_tsvector_codec import AsyncpgTsvectorCodec
from hare.dialects.postgresql.drivers.asyncpg.constants import (
    ASYNCPG_EXTENSION_TYPE_NAMES,
    ASYNCPG_EXTENSION_TYPES_SQL,
    EXTENSION_STATEMENT_WORD,
    LTREE_BINARY_VERSION,
)


class AsyncpgExtensionTypeCodecs:
    """The binary codecs of the types asyncpg would read as text - ``tsvector`` and the types of the
    ``citext``, ``ltree`` and ``hstore`` extensions. asyncpg's text array parser crashes the process on
    an empty value in an array of such a type (``ARRAY['']::citext[]``); read in binary, the same text
    comes back without it."""

    @staticmethod
    def encode_ltree(value: str) -> bytes:
        """Encodes an ``ltree`` value - its version byte, then its UTF-8 text.

        Raises:
            TypeError: The value isn't a string.
        """
        if not isinstance(value, str):
            raise TypeError(f"expected str, got {type(value).__name__}")
        return bytes((LTREE_BINARY_VERSION,)) + value.encode()

    @staticmethod
    def decode_ltree(value: bytes) -> str:
        """Decodes an ``ltree`` value, after its version byte."""
        return value[1:].decode()

    @staticmethod
    def changes_extensions(sql: str) -> bool:
        """Whether a script may create or drop an extension - its types are then looked up again.

        Args:
            sql: The script.

        Returns:
            True when the script names an extension.
        """
        return EXTENSION_STATEMENT_WORD in sql.upper()

    @classmethod
    async def install(cls, connection: asyncpg.Connection) -> None:
        """Registers the codecs on a connection - ``tsvector`` always, an extension's types when the
        database has them.

        Args:
            connection: The connection.
        """
        await connection.set_type_codec(
            "tsvector",
            schema="pg_catalog",
            encoder=AsyncpgTsvectorCodec.encode,
            decoder=AsyncpgTsvectorCodec.get_decoder(),
            format="binary",
        )
        codecs = {
            "ltree": (cls.encode_ltree, cls.decode_ltree),
            "hstore": (AsyncpgHstoreCodec.encode, AsyncpgHstoreCodec.get_decoder()),
        }
        for type_name, schema in await connection.fetch(
            ASYNCPG_EXTENSION_TYPES_SQL, list(ASYNCPG_EXTENSION_TYPE_NAMES)
        ):
            if type_name == "citext":
                # citext's binary form is text's - asyncpg's own text codec reads it.
                await connection.set_builtin_type_codec(type_name, schema=schema, codec_name="text")
                continue
            encoder, decoder = codecs[type_name]
            await connection.set_type_codec(
                type_name, schema=schema, encoder=encoder, decoder=decoder, format="binary"
            )
