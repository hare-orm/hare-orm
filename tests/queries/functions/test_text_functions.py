"""Text functions with Postgres semantics on every backend."""

import hashlib
from typing import Any

import pytest

from hare.contrib.test import requires_features
from hare.exceptions import OperationalError, QueryError
from hare.query.expressions import F
from hare.query.functions import (
    MD5,
    SHA1,
    SHA224,
    SHA256,
    SHA384,
    SHA512,
    Chr,
    Coalesce,
    Left,
    Length,
    Lower,
    LPad,
    LTrim,
    Ord,
    Repeat,
    Replace,
    Reverse,
    Right,
    RPad,
    RTrim,
    StrIndex,
    Substr,
    Trim,
    Upper,
)
from hare.transactions.transactions import Transactions
from tests.shared_models.testmodels import CharFields


async def get_value(row: CharFields, expression: Any) -> Any:
    return (await CharFields.objects.filter(id=row.id).annotate(value=expression).values_list("value", flat=True))[0]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_slicing_functions(db):
    """SUBSTR is the database's own - SQLite counts a negative start from the end and takes a negative
    length as the characters before, PostgreSQL counts positions before 1 as empty and refuses it."""
    row = await CharFields.objects.create(id=1, char="héllo🎉")
    is_sqlite = CharFields._meta.connection.dialect.name == "sqlite"

    assert await get_value(row, Left("char", 2)) == "hé"
    assert await get_value(row, Left("char", -2)) == "héll"
    assert await get_value(row, Right("char", 2)) == "o🎉"
    assert await get_value(row, Right("char", -2)) == "llo🎉"
    assert await get_value(row, Right("char", 20)) == "héllo🎉"
    assert await get_value(row, Substr("char", 2, 3)) == "éll"
    assert await get_value(row, Substr("char", 4)) == "lo🎉"
    assert await get_value(row, Substr("char", -1, 3)) == ("🎉" if is_sqlite else "h")
    assert await get_value(row, StrIndex("char", "l")) == 3
    assert await get_value(row, StrIndex("char", "z")) == 0
    if is_sqlite:
        assert await get_value(row, Substr("char", 1, -1)) == ""
    else:
        with pytest.raises(OperationalError):
            async with Transactions.atomic():
                await get_value(row, Substr("char", 1, -1))


@pytest.mark.asyncio
async def test_building_functions_follow_postgres(db):
    row = await CharFields.objects.create(id=1, char="  ab  ")

    assert await get_value(row, LTrim("char")) == "ab  "
    assert await get_value(row, RTrim("char")) == "  ab"
    assert await get_value(row, Replace("char", " ", "_")) == "__ab__"
    assert await get_value(row, Replace("char", " ")) == "ab"
    assert await get_value(row, Replace("char", F("char"), "x")) == "x"
    assert await get_value(row, Repeat("char", 2)) == "  ab    ab  "
    assert await get_value(row, Repeat("char", -1)) == ""
    assert await get_value(row, Reverse(RTrim("char"))) == "ba  "
    assert await get_value(row, LPad("char", 8, "*")) == "**  ab  "
    assert await get_value(row, RPad(LTrim("char"), 6, "xy")) == "ab  xy"
    assert await get_value(row, LPad("char", 3)) == "  a"
    assert await get_value(row, LPad("char", 9)) == "     ab  "
    assert await get_value(row, Ord("char")) == 32
    assert await get_value(row, Chr(233)) == "é"


@pytest.mark.asyncio
async def test_digests_of_the_utf8_text(db):
    row = await CharFields.objects.create(id=1, char="héllo")
    connection = CharFields._meta.connection
    if connection.dialect.name == "postgresql":
        await connection.execute_script("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    for function, algorithm in (
        (MD5, "md5"),
        (SHA1, "sha1"),
        (SHA224, "sha224"),
        (SHA256, "sha256"),
        (SHA384, "sha384"),
        (SHA512, "sha512"),
    ):
        assert await get_value(row, function("char")) == hashlib.new(algorithm, b"h\xc3\xa9llo").hexdigest()


@pytest.mark.asyncio
async def test_text_function_in_a_filter(db):
    await CharFields.objects.create(id=1, char="apple")
    await CharFields.objects.create(id=2, char="banana")

    assert await CharFields.objects.annotate(first=Left("char", 1)).filter(first="b").values_list("id", flat=True) == [
        2
    ]
    assert await CharFields.objects.annotate(position=StrIndex("char", "an")).filter(position__gt=0).count() == 1


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_a_text_longer_than_the_database_holds_is_refused(db):
    """SQLite built the text - a count in the billions took the process down allocating it."""
    row = await CharFields.objects.create(id=1, char="abc")

    for expression in (Repeat("char", 400_000_000), LPad("char", 300_000_000), RPad("char", 300_000_000)):
        # PostgreSQL 14 refuses the allocation itself - "invalid memory alloc request size".
        refusals = "requested length too large|string or blob too big|invalid memory alloc request size"
        with pytest.raises(OperationalError, match=refusals):
            async with Transactions.atomic():
                await get_value(row, expression)


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_an_integer_expression_argument(db):
    """PostgreSQL has no text function taking a bigint - an integer expression was refused there."""
    row = await CharFields.objects.create(id=1, char="abc")

    assert await get_value(row, Repeat("char", F("id") + 1)) == "abcabc"
    assert await get_value(row, Left("char", F("id") * 2)) == "ab"
    with pytest.raises(OperationalError, match="integer out of range|string or blob too big"):
        async with Transactions.atomic():
            await get_value(row, Repeat("char", F("id") * 3_000_000_000))


@pytest.mark.database_independent
def test_text_function_rejects_a_wrong_argument():
    with pytest.raises(QueryError, match="takes 2 argument"):
        Left("char")
    with pytest.raises(QueryError, match="expected an integer or an expression"):
        Left("char", 1.5)


@pytest.mark.database_independent
@pytest.mark.parametrize(
    "build",
    [
        lambda: Upper("char", "x"),
        lambda: Lower("char", 1),
        lambda: Trim("char", "x"),
        lambda: Length("char", "x"),
        lambda: Coalesce("char"),
    ],
)
def test_a_function_given_the_wrong_number_of_arguments_raises_query_error(build):
    """Upper("a", "b") took "b" as the SQL alias and Lower("a", 1) dropped its 1; the others reached
    the database - which refused them, or, on PostgreSQL, took Trim's second as the characters."""
    with pytest.raises(QueryError, match="argument"):
        build()


@pytest.mark.database_independent
def test_a_spatial_function_given_the_wrong_number_of_arguments_raises_query_error():
    from hare.gis.functions import Area, Distance

    with pytest.raises(QueryError, match="takes 1 argument"):
        Area("shape", "x")
    with pytest.raises(QueryError, match="takes 2 argument"):
        Distance("shape")


@pytest.mark.database_independent
@pytest.mark.parametrize(
    "build",
    [
        lambda: Left("char", "b"),
        lambda: Right("char", "2"),
        lambda: Substr("char", 1, "2"),
        lambda: Repeat("char", "3"),
        lambda: LPad("char", "5"),
        lambda: RPad("char", 5, 0),
        lambda: StrIndex("char", 1),
        lambda: Replace("char", 1),
    ],
)
def test_a_text_function_given_a_literal_of_the_wrong_type_raises_query_error(build):
    """A text where a length, a position or a count goes, or a number where text goes, reached the
    database - which has no such function (PostgreSQL), or failed inside hare's own (SQLite)."""
    with pytest.raises(QueryError, match="expected"):
        build()
