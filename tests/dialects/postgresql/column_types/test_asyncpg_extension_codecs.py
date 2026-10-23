"""The binary codecs asyncpg connections read tsvector and hstore through: the text they give is the
one PostgreSQL prints, compiled or not, and a tsvector text is read as tsvectorin reads it."""

import pytest

from hare.dialects.postgresql.drivers.asyncpg.asyncpg_hstore_codec import AsyncpgHstoreCodec
from hare.dialects.postgresql.drivers.asyncpg.asyncpg_tsvector_codec import AsyncpgTsvectorCodec

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
