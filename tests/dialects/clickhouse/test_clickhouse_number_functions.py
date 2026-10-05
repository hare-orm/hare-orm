"""Number and text functions behave as on PostgreSQL where ClickHouse's own differ: a division by zero is
refused, a quotient of integers is an integer rounded towards zero, a Decimal quotient and remainder keep their
digits, exp() and ln() are accurate, a float's
half rounds away from zero, a math function refuses a value outside its domain but not a NULL row,
chr() writes any character and substr() counts positions before the first one."""

import decimal
import math

import pytest

from hare.exceptions import OperationalError
from hare.query.expressions import F, Value
from hare.query.functions import Avg, Chr, Concat, Exp, Ln, Mod, Ord, Power, Round, Sqrt, Substr, Sum
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
async def test_a_quotient_of_integers_is_an_integer_rounded_towards_zero(clickhouse_db):
    await Player.objects.bulk_create([Player(id=player_id, name=str(player_id)) for player_id in (7, 8, 10)])
    assert await get_values((F("id") - 14) / 2) == [-3, -3, -2]
    # Inside an aggregate too - ClickHouse's "/" would average 25/9 over the float quotients.
    assert await Player.objects.aggregate(average=Avg(F("id") / 3), total=Sum(F("id") / 3)) == {
        "average": pytest.approx(7 / 3),
        "total": 7,
    }


@pytest.mark.asyncio
async def test_a_division_by_zero_is_refused(clickhouse_db):
    await create_players()

    for expression in (
        F("id") / (F("id") - 1),
        F("score") / decimal.Decimal("0.00"),
        F("score") / F("score"),
        F("rating") / 0.0,
    ):
        with pytest.raises(OperationalError, match="division by zero"):
            await get_values(expression)


@pytest.mark.asyncio
async def test_a_null_dividend_or_divisor_divides_into_null(clickhouse_db):
    await create_players()

    assert await get_values(F("rating") / (F("id") - 2)) == [-2.5, None]
    assert await get_values(F("id") / F("rating")) == [0.4, None]


@pytest.mark.asyncio
async def test_a_decimal_quotient_keeps_its_digits(clickhouse_db):
    await create_players()

    assert await get_values(F("score") / 8, id=1) == [decimal.Decimal("1.5425")]
    assert await get_values(F("score") / decimal.Decimal("0.3"), id=1) == [decimal.Decimal("41.13333333333333333333")]


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
    assert await get_values(Exp(F("score") / 100), id=1) == [pytest.approx(decimal.Decimal(math.exp(0.1234)))]


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0.5, -20.3, 123.456, 700.0, -740.0])
async def test_exp_is_within_a_unit_in_the_last_place(clickhouse_db, value):
    await create_players()

    (result,) = await get_values(Exp(Value(value)), id=1)
    assert abs(result - math.exp(value)) <= math.ulp(math.exp(value))


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [1e-300, 0.75, 2.0, 123456.789, 1e300])
async def test_ln_is_within_a_unit_in_the_last_place(clickhouse_db, value):
    await create_players()

    (result,) = await get_values(Ln(Value(value)), id=1)
    assert abs(result - math.log(value)) <= math.ulp(math.log(value))


@pytest.mark.asyncio
async def test_a_math_function_refuses_a_value_outside_its_domain_but_not_a_null(clickhouse_db):
    await create_players()

    for expression in (Ln("rating"), Sqrt("rating"), Exp("rating"), Power("rating", 0.5)):
        assert await get_values(expression, id=2) == [None]
    for expression, message in (
        (Ln(F("rating") - 2.5), "cannot take logarithm of zero"),
        (Sqrt(F("rating") * -1), "cannot take square root of a negative number"),
        (Exp(F("rating") * 1000), "value out of range: overflow"),
        (Exp(F("rating") * -1000), "value out of range: underflow"),
        (Power(F("rating") * -1, 0.5), "a negative number raised to a non-integer power"),
        (Power(F("rating") - 2.5, -1), "zero raised to a negative power is undefined"),
    ):
        with pytest.raises(OperationalError, match=message):
            await get_values(expression)


@pytest.mark.asyncio
async def test_a_float_half_rounds_away_from_zero(clickhouse_db):
    await create_players()

    assert await get_values(Round("rating"), id=1) == [3.0]
    assert await get_values(Round(F("rating") * -1), id=1) == [-3.0]
    assert await get_values(Round(F("rating") / 10, 1), id=1) == [0.3]


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
    for code_point, message in (
        (0, "null character not permitted"),
        (-1, "character number must be positive"),
        (55296, "requested character not valid for encoding"),
        (1114112, "requested character too large for encoding"),
    ):
        with pytest.raises(OperationalError, match=message):
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
async def test_substr_counts_positions_before_the_first_one(clickhouse_db):
    await create_players()

    assert await get_values(Substr("name", 0, 2), id=1) == ["H"]
    assert await get_values(Substr("name", -1, 3), id=1) == ["H"]
    assert await get_values(Substr("name", 2, 3), id=1) == ["ell"]
    assert await get_values(Substr("name", 0), id=1) == ["Hello"]
    with pytest.raises(OperationalError, match="negative substring length not allowed"):
        await get_values(Substr("name", 1, -1), id=1)


@pytest.mark.asyncio
async def test_a_float_concatenated_is_postgres_text_of_it(clickhouse_db):
    await create_players()

    assert await get_values(Concat(F("rating") * 1e15, Value("|")), id=1) == ["2.5e+15|"]
    assert await get_values(Concat(F("rating") * 1e-7, Value("|")), id=1) == ["2.5e-07|"]
    assert await get_values(Concat(F("rating") * 4e14, Value("|")), id=1) == ["1e+15|"]
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
    with pytest.raises(OperationalError, match="division by zero"):
        await (
            Revision.objects.filter(pk=(1, 1))
            .annotate(value=F("score") / (F("number") - 1))
            .values_list("value", flat=True)
        )
    assert await Revision.objects.filter(pk=(1, 1)).annotate(value=F("score") % (F("number") + 1)).values_list(
        "value", flat=True
    ) == [1.0]


@pytest.mark.asyncio
async def test_an_integer_result_out_of_its_type_range_is_refused(clickhouse_db):
    await Player.objects.create(id=9_223_372_036_854_775_806, name="edge")
    with pytest.raises(OperationalError, match="bigint out of range"):
        await get_values(F("id") + 2, name="edge")
    assert await get_values(F("id") + 1, name="edge") == [9_223_372_036_854_775_807]
