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
    Left,
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
)
from hare.transactions.transactions import Transactions
from tests.testmodels import CharFields


async def get_value(row: CharFields, expression: Any) -> Any:
    return (await CharFields.objects.filter(id=row.id).annotate(value=expression).values_list("value", flat=True))[0]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_slicing_functions_follow_postgres(db):
    row = await CharFields.objects.create(id=1, char="héllo🎉")

    assert await get_value(row, Left("char", 2)) == "hé"
    assert await get_value(row, Left("char", -2)) == "héll"
    assert await get_value(row, Right("char", 2)) == "o🎉"
    assert await get_value(row, Right("char", -2)) == "llo🎉"
    assert await get_value(row, Right("char", 20)) == "héllo🎉"
    assert await get_value(row, Substr("char", 2, 3)) == "éll"
    assert await get_value(row, Substr("char", 4)) == "lo🎉"
    assert await get_value(row, Substr("char", -1, 3)) == "h"
    assert await get_value(row, StrIndex("char", "l")) == 3
    assert await get_value(row, StrIndex("char", "z")) == 0
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


def test_text_function_rejects_a_wrong_argument():
    with pytest.raises(QueryError, match="takes 2 argument"):
        Left("char")
    with pytest.raises(QueryError, match="expected text, an integer or an expression"):
        Left("char", 1.5)
