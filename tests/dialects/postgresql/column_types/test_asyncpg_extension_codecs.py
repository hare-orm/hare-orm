"""The binary codecs asyncpg connections read tsvector and hstore through: the text they give is the
one PostgreSQL prints, compiled or not, and a tsvector text is read as tsvectorin reads it."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from hare.dialects.postgresql.drivers.asyncpg.asyncpg_hstore_codec import AsyncpgHstoreCodec
from hare.dialects.postgresql.drivers.asyncpg.asyncpg_tsvector_codec import AsyncpgTsvectorCodec
from hare.dialects.postgresql.drivers.asyncpg.client.asyncpg_client import AsyncpgClient


@pytest.mark.database_independent
@pytest.mark.asyncio
@pytest.mark.parametrize("old_oids,new_oids", [({10}, {10}), ({10}, {10, 20}), ({10, 20}, {10})])
async def test_schema_generation_refreshes_codecs_and_retires_only_removed_types(old_oids, new_oids):
    client = AsyncpgClient(connection_alias="probe", database="unused")
    pool = MagicMock(expire_connections=AsyncMock())
    connection = MagicMock(
        reload_schema_state=AsyncMock(),
        set_type_codec=AsyncMock(),
        set_builtin_type_codec=AsyncMock(),
    )
    connection.get_server_pid.return_value = 7
    reads = []

    async def catalog(*args):
        reads.append(client.schema_generation)
        if len(reads) == 1:
            client.schema_generation += 1
        return [("citext", f"types_{oid}", oid) for oid in sorted(new_oids)]

    connection.fetch = AsyncMock(side_effect=catalog)
    client.schema_connections.get_bucket(client).store((id(pool), 7), (0, frozenset(old_oids)))
    client.schema_generation = 1
    await client.refresh_schema_codecs(connection, pool)
    await client.refresh_schema_codecs(connection, pool)
    await client.refresh_schema_codecs(connection, pool)
    assert reads == [1, 2]
    assert connection.reload_schema_state.await_count == 2
    assert pool.expire_connections.await_count == bool(old_oids - new_oids)


@pytest.mark.database_independent
@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["transaction_end", "savepoint_rollback"])
async def test_pinned_connection_refreshes_after_schema_changes_become_visible(boundary):
    client = AsyncpgClient(connection_alias="probe", database="unused")
    pool = MagicMock(expire_connections=AsyncMock(), release=AsyncMock())
    connection = MagicMock(
        reload_schema_state=AsyncMock(),
        set_type_codec=AsyncMock(),
        set_builtin_type_codec=AsyncMock(),
        fetch=AsyncMock(return_value=[]),
        execute=AsyncMock(),
    )
    connection.get_server_pid.return_value = 7
    connection.is_closed.return_value = False
    client._pool = pool
    transaction = client._get_transaction_client()
    transaction._connection = connection
    transaction._held_pool = pool
    transaction.transaction = MagicMock()
    client.schema_connections.get_bucket(client).store((id(pool), 7), (0, frozenset()))
    transaction._schema_touched = True
    client.schema_generation = 1
    async with transaction.acquire_connection():
        assert transaction._lock.locked()
    assert connection.fetch.await_count == 1
    if boundary == "transaction_end":
        await transaction._give_back_transaction_resources()
        pool.release.assert_awaited_once()
    else:
        await transaction._driver_rollback_to_savepoint("nested")
    assert client.schema_generation == 2
    await client.refresh_schema_codecs(connection, pool)
    assert connection.fetch.await_count == 2
    pool.expire_connections.assert_not_awaited()


#: tsvector input -> the text PostgreSQL prints of it.
TSVECTOR_TEXTS = {
    "": "",
    "a b a:3": "'a':3 'b'",
    "'a'b": "'a' 'b'",
    "a'b": "'a''b'",
    ":a": "':a'",
    "a:1A2": "'a':1A",
    "a:1DA": "'a':1A",
    "a:1*": "'a':1A",
    "a:1A,1B": "'a':1A",
    "a:1 a:2": "'a':1,2",
    "a:99999": "'a':16383",
    "a:16383,16384": "'a':16383",
    "b\\\\'c:2,3 日本:3B": "'b\\\\''c':2,3 '日本':3B",
}


@pytest.mark.parametrize(("text", "printed"), list(TSVECTOR_TEXTS.items()))
def test_a_tsvector_text_is_read_and_printed_as_postgresql_does(text, printed):
    binary = AsyncpgTsvectorCodec.encode(text)
    assert AsyncpgTsvectorCodec.decode(binary) == printed
    if AsyncpgTsvectorCodec.native_decoder is not None:
        assert AsyncpgTsvectorCodec.native_decoder(binary) == printed


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("a:1AD", 'syntax error in tsvector: "a:1AD"'),
        ("a:0", 'wrong position info in tsvector: "a:0"'),
        ("a:", 'syntax error in tsvector: "a:"'),
        ("''", "syntax error in tsvector: \"''\""),
        ("a\\", 'there is no escaped character: "a\\"'),
        ("x" * 2047, "word is too long (2047 bytes, max 2046 bytes)"),
    ],
)
def test_a_text_that_isnt_a_tsvector_is_refused_with_postgresqls_message(text, message):
    with pytest.raises(ValueError) as error:
        AsyncpgTsvectorCodec.encode(text)
    assert str(error.value) == message


@pytest.mark.database_independent
def test_an_hstore_reads_back_as_the_text_postgresql_prints():
    binary = AsyncpgHstoreCodec.encode('"q\\"x"=>"\\\\", n=>NULL')
    printed = '"q\\"x"=>"\\\\", "n"=>NULL'
    assert AsyncpgHstoreCodec.decode(binary) == printed
    if AsyncpgHstoreCodec.native_decoder is not None:
        assert AsyncpgHstoreCodec.native_decoder(binary) == printed
    assert AsyncpgHstoreCodec.decode(AsyncpgHstoreCodec.encode({})) == ""
