"""Number and text functions behave as on PostgreSQL where ClickHouse's own differ: a division by zero is
refused, a quotient of integers is an integer rounded towards zero, a Decimal quotient and remainder keep their
digits, exp() and ln() are accurate, a float's
half rounds away from zero, a math function refuses a value outside its domain but not a NULL row,
chr() writes any character and substr() counts positions before the first one."""

import decimal
import math

import pytest

from hare.exceptions import OperationalError
from hare.query.expressions import Case, F, Value, When
from hare.query.functions import (
    Avg,
    Chr,
    Coalesce,
    Concat,
    Count,
    Exp,
    Greatest,
    Length,
    Mod,
    Ord,
    Power,
    Round,
    StrIndex,
    Substr,
    Sum,
)
from tests.dialects.clickhouse.models import Player, Revision


async def get_values(expression, **filters):
    return (
        await Player.objects.filter(**filters)
        .order_by("id")
        .annotate(value=expression)
        .values_list("value", flat=True)
    )


async def create_players():
    await Player.objects.bulk_create(
        [
            Player(id=1, name="Hello", score=decimal.Decimal("12.34"), rating=2.5),
            Player(id=2, name="Null", score=decimal.Decimal("0"), rating=None),
        ]
    )


@pytest.mark.asyncio
async def test_a_quotient_of_integers_is_clickhouses_own(clickhouse_db):
    """ClickHouse's "/" of integers gives a fraction - read as the server gives it."""
    await Player.objects.bulk_create([Player(id=player_id, name=str(player_id)) for player_id in (7, 8, 10)])
    assert await get_values((F("id") - 14) / 2) == [-3.5, -3, -2]
    assert await Player.objects.aggregate(average=Avg(F("id") / 3), total=Sum(F("id") / 3)) == {
        "average": pytest.approx(25 / 9),
        "total": pytest.approx(25 / 3),
    }


@pytest.mark.asyncio
async def test_a_null_dividend_or_divisor_divides_into_null(clickhouse_db):
    await create_players()

    assert await get_values(F("rating") / (F("id") - 2)) == [-2.5, None]
    assert await get_values(F("id") / F("rating")) == [0.4, None]


@pytest.mark.asyncio
async def test_a_decimal_quotient_has_the_dividends_scale_as_clickhouse_divides(clickhouse_db):
    await create_players()

    assert await get_values(F("score") / 8, id=1) == [decimal.Decimal("1.54")]
    assert await get_values(F("score") / decimal.Decimal("0.3"), id=1) == [decimal.Decimal("41.13")]


@pytest.mark.asyncio
async def test_a_decimal_remainder_is_exact(clickhouse_db):
    await create_players()

    remainders = [
        await get_values(expression, id=1)
        for expression in (
            F("score") % 7,
            Mod("score", 7),
            F("score") % decimal.Decimal("0.1"),
            Mod("score", decimal.Decimal("0.1")),
            F("id") % decimal.Decimal("0.3"),
        )
    ]
    assert [str(value) for (value,) in remainders] == ["5.34", "5.34", "0.04", "0.04", "0.1"]


@pytest.mark.asyncio
async def test_power_and_exp_take_a_decimal(clickhouse_db):
    await create_players()

    assert await get_values(Power("score", 2), id=1) == [pytest.approx(decimal.Decimal("152.2756"))]
    assert await get_values(Exp(F("score") / 100), id=1) == [pytest.approx(decimal.Decimal(math.exp(0.12)))]


@pytest.mark.asyncio
async def test_a_decimal_half_rounds_away_from_zero(clickhouse_db):
    await Player.objects.bulk_create(
        [
            Player(id=1, name="Up", score=decimal.Decimal("7.35")),
            Player(id=2, name="Down", score=decimal.Decimal("-7.35")),
        ]
    )

    assert await get_values(Round("score", 1)) == [decimal.Decimal("7.4"), decimal.Decimal("-7.4")]
    assert await get_values(Round(F("score") * 1, 1)) == [decimal.Decimal("7.4"), decimal.Decimal("-7.4")]


@pytest.mark.asyncio
async def test_chr_writes_any_character_and_refuses_no_character(clickhouse_db):
    await create_players()

    assert await get_values(Chr(Value(65)), id=1) == ["A"]
    assert await get_values(Chr(Value(1046)), id=1) == ["Ж"]
    assert await get_values(Chr(Value(8364)), id=1) == ["€"]
    assert await get_values(Chr(Value(128512)), id=1) == ["\U0001f600"]
    assert await get_values(Chr(Value(0)), id=1) == ["\x00"]
    for code_point in (-1, 55296, 1114112):
        with pytest.raises(OperationalError, match="no character has the code point"):
            await get_values(Chr(Value(code_point)), id=1)


@pytest.mark.asyncio
async def test_ord_reads_the_code_point_of_the_first_character(clickhouse_db):
    """ascii() gives the first byte - 196 of İ, 230 of 日."""
    await create_players()

    for text, code_point in (
        ("A", 65),
        ("İstanbul", 304),
        ("ß", 223),
        ("日本語", 26085),
        ("\U0001f600!", 128512),
        ("", 0),
    ):
        assert await get_values(Ord(Value(text)), id=1) == [code_point]
    assert await get_values(Ord("name"), id=1) == [72]
    assert await get_values(Ord(Value(None)), id=1) == [None]


@pytest.mark.asyncio
async def test_substr_is_clickhouse_substring(clickhouse_db):
    """substringUTF8() - a negative start counts from the end, a negative length leaves that many
    characters off the end, a start of 0 is refused."""
    await create_players()

    assert await get_values(Substr("name", -1, 3), id=1) == ["o"]
    assert await get_values(Substr("name", 2, 3), id=1) == ["ell"]
    assert await get_values(Substr("name", 1, -1), id=1) == ["Hell"]
    with pytest.raises(OperationalError, match="1-based"):
        await get_values(Substr("name", 0, 2), id=1)


@pytest.mark.asyncio
async def test_a_float_concatenated_is_clickhouse_text_of_it(clickhouse_db):
    await create_players()

    assert await get_values(Concat(F("rating") * 1e15, Value("|")), id=1) == ["2500000000000000|"]
    assert await get_values(Concat(F("rating") * 1e-7, Value("|")), id=1) == ["2.5e-7|"]
    assert await get_values(Concat(F("rating") * 4e14, Value("|")), id=1) == ["1000000000000000|"]
    assert await get_values(Concat(F("rating") * 3, Value("|")), id=1) == ["7.5|"]


@pytest.mark.asyncio
async def test_a_division_over_a_composite_key_model(clickhouse_db):
    await Revision.objects.bulk_create(
        [
            Revision(document_id=1, number=1, title="first", score=3.0),
            Revision(document_id=1, number=2, title="second", score=None),
        ]
    )

    assert await Revision.objects.filter(pk=(1, 2)).annotate(value=F("score") / (F("number") - 2)).values_list(
        "value", flat=True
    ) == [None]
    assert await Revision.objects.filter(pk=(1, 1)).annotate(value=F("score") % (F("number") + 1)).values_list(
        "value", flat=True
    ) == [1.0]


@pytest.mark.asyncio
async def test_an_integer_result_past_its_type_wraps_around_as_clickhouse_computes_it(clickhouse_db):
    """ClickHouse's own integer arithmetic - a result past the type wraps around, hare checks nothing."""
    await Player.objects.create(id=9_223_372_036_854_775_806, name="edge")
    assert await get_values(F("id") + 2, name="edge") == [-9_223_372_036_854_775_808]
    assert await get_values(F("id") + 1, name="edge") == [9_223_372_036_854_775_807]


@pytest.mark.asyncio
async def test_nested_integer_arithmetic_is_written_once(clickhouse_db):
    await Player.objects.create(id=9_223_372_036_854_775_800, name="edge")
    deep = F("id")
    for _ in range(8):
        deep = deep - 1
    # Each level wrote the level below three times - 8 levels were megabytes of SQL.
    assert len(Player.objects.annotate(value=deep).sql()) < 10_000
    assert await get_values(deep, name="edge") == [9_223_372_036_854_775_792]
    # Past the type's range on an inner level, back within it on the outer one - wrapped around twice.
    assert await get_values(F("id") + 10 - 20, name="edge") == [9_223_372_036_854_775_790]


@pytest.mark.asyncio
async def test_integer_arithmetic_combines_with_a_float(clickhouse_db):
    await Player.objects.create(id=3, name="mixed", rating=0.5)
    assert await get_values(F("id") * F("id") + F("rating"), name="mixed") == [9.5]
    assert await get_values(F("id") * 2 * F("rating"), name="mixed") == [3.0]
    assert await get_values(F("rating") - (F("id") + 1), name="mixed") == [-3.5]
    assert await get_values((F("id") * 2) % F("rating"), name="mixed") == [0.0]


@pytest.mark.asyncio
async def test_a_count_or_a_length_combines_with_a_signed_integer(clickhouse_db):
    """ClickHouse returns them as UInt64, which has no common type with a signed integer - COALESCE, CASE and
    greatest() failed with code 386."""
    await Revision.objects.create(document_id=1, number=5, title="abc")
    revisions = Revision.objects.filter(document_id=1)
    assert await revisions.annotate(value=Coalesce("number", Length("title"))).values_list("value", flat=True) == [5]
    assert await revisions.annotate(value=Greatest("number", StrIndex("title", "c"))).values_list(
        "value", flat=True
    ) == [5]
    assert await revisions.annotate(value=Case(When(number=5, then=Length("title")), default=F("number"))).values_list(
        "value", flat=True
    ) == [3]
    assert await revisions.values("document_id").annotate(
        value=Greatest(F("document_id"), Count("title"))
    ).values_list("value", flat=True) == [1]
