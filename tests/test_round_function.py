"""Round(field_or_expression, precision) - database-side rounding with the same result type and
digits on every backend."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio

from hare.exceptions import QueryError
from hare.query.expressions import Case, F, Value, When
from hare.query.functions import Avg, Round, Sum
from tests.testmodels import Author, Book, ExpressionTypeParity, FloatFields


@pytest_asyncio.fixture
async def rounding_rows(db):
    for num, dec in ((7, Decimal("1.10")), (2, Decimal("0.40"))):
        await ExpressionTypeParity.objects.create(num=num, big=1, dec=dec, flag=True, grp="a", ts=datetime.now(UTC))


async def rounded_values(expression) -> list:
    return await ExpressionTypeParity.objects.all().annotate(c=expression).order_by("num").values_list("c", flat=True)


def assert_same_values(actual: list, expected: list) -> None:
    assert actual == expected
    assert [type(value) for value in actual] == [type(value) for value in expected]
    assert [str(value) for value in actual] == [str(value) for value in expected]


@pytest.mark.asyncio
async def test_round_decimal_quotient_to_precision(rounding_rows):
    assert_same_values(await rounded_values(Round(F("dec") / 3, 2)), [Decimal("0.13"), Decimal("0.37")])
    assert_same_values(await rounded_values(Round(F("dec") / 3, 3)), [Decimal("0.133"), Decimal("0.367")])


@pytest.mark.asyncio
async def test_round_decimal_field_and_default_precision(rounding_rows):
    assert_same_values(await rounded_values(Round("dec", 1)), [Decimal("0.4"), Decimal("1.1")])
    assert_same_values(await rounded_values(Round("dec")), [Decimal("0"), Decimal("1")])


@pytest.mark.asyncio
async def test_round_integer_stays_integer(rounding_rows):
    assert_same_values(await rounded_values(Round("num")), [2, 7])
    assert_same_values(await rounded_values(Round(F("num") * 2, 1)), [4, 14])


@pytest.mark.asyncio
async def test_round_float_stays_float(db):
    await FloatFields.objects.create(floatnum=2.345)
    values = await FloatFields.objects.all().annotate(c=Round("floatnum", 1)).values_list("c", flat=True)
    assert values == [2.3]
    assert type(values[0]) is float


@pytest.mark.asyncio
async def test_round_filter_order_and_aggregate(rounding_rows):
    queryset = ExpressionTypeParity.objects.all().annotate(c=Round(F("dec") / 3, 2))
    assert await queryset.filter(c=Decimal("0.37")).values_list("num", flat=True) == [7]
    assert await queryset.order_by("-c").values_list("num", flat=True) == [7, 2]
    aggregated = await ExpressionTypeParity.objects.all().aggregate(
        average=Round(Avg("dec"), 1), total=Round(Sum("dec"), 0)
    )
    assert_same_values([aggregated["average"], aggregated["total"]], [Decimal("0.8"), Decimal("2")])


@pytest.mark.asyncio
async def test_round_in_update(rounding_rows):
    await ExpressionTypeParity.objects.filter(num=7).update(dec=Round(F("dec") / 3, 2))
    assert await ExpressionTypeParity.objects.get(num=7).values_list("dec", flat=True) == Decimal("0.37")


@pytest.mark.asyncio
async def test_round_precision_is_part_of_the_cached_query_shape(rounding_rows):
    for precision, expected in ((2, "0.37"), (3, "0.367"), (2, "0.37")):
        values = (
            await ExpressionTypeParity.objects.filter(num=7)
            .annotate(c=Round(F("dec") / 3, precision))
            .values_list("c", flat=True)
        )
        assert_same_values(values, [Decimal(expected)])


@pytest.mark.parametrize("precision", [-1, 1.5, "2", True, 1001])
def test_round_rejects_invalid_precision(precision):
    with pytest.raises(QueryError):
        Round("dec", precision)


@pytest.mark.asyncio
async def test_round_of_decimal_value_literal_is_decimal(db):
    await FloatFields.objects.create(floatnum=1.0)
    rounded = await FloatFields.objects.annotate(rounded=Round(Value(Decimal("1.555")), 2)).values_list(
        "rounded", flat=True
    )
    assert type(rounded[0]) is Decimal
    assert rounded[0].as_tuple().exponent == -2
    assert rounded[0] in (Decimal("1.55"), Decimal("1.56"))


@pytest.mark.asyncio
async def test_round_over_aggregate_is_grouped_and_filtered_in_having(db):
    """Round(<aggregate>, n) is itself an aggregate - its precision literal used to vote
    "not an aggregate", so the query had no GROUP BY and a filter on it landed in WHERE."""
    first = await Author.objects.create(name="first")
    second = await Author.objects.create(name="second")
    empty = await Author.objects.create(name="empty")
    await Book.objects.create(name="a", author=first, rating=1)
    await Book.objects.create(name="b", author=first, rating=2)
    await Book.objects.create(name="c", author=second, rating=4)

    rounded = (
        await Author.objects.annotate(average=Round(Avg("books__rating"), 1))
        .order_by("id")
        .values_list("id", "average")
    )
    filtered = (
        await Author.objects.annotate(average=Round(Avg("books__rating"), 0)).filter(average__gt=2).order_by("id")
    )

    assert rounded == [(first.id, 1.5), (second.id, 4.0), (empty.id, None)]
    assert [author.id for author in filtered] == [second.id]


@pytest.mark.asyncio
async def test_round_of_untyped_numbers_keeps_integer_or_float(rounding_rows):
    assert_same_values(await rounded_values(Round(F("num") * 0.5, 1)), [1.0, 3.5])
    assert_same_values(await rounded_values(Round(Value(2.5))), [3.0, 3.0])
    assert_same_values(await rounded_values(Round(Value(3), 1)), [3, 3])
    assert_same_values(await rounded_values(Round(Case(When(num=2, then=F("num")), default=0.5))), [2.0, 1.0])
