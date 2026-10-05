from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
import pytest_asyncio

from hare.query.expressions import Case, F, When
from hare.query.functions import Avg, Coalesce, Length, Lower, Max, Min, Sum, Trim, Upper
from tests.testmodels import CharFields, DateFields, DatetimeFields, DecimalFields


@pytest_asyncio.fixture
async def char_rows(db):
    await CharFields.objects.create(char="abc")
    await CharFields.objects.create(char="a")


@pytest.mark.asyncio
async def test_max_of_length_is_an_integer(char_rows):
    """Max(Length("char")) used to decode through the CharField argument of Length: '3'."""
    for _ in range(2):  # cache miss, then cache hit
        assert await CharFields.objects.all().aggregate(result=Max(Length("char"))) == {"result": 3}


@pytest.mark.asyncio
async def test_min_sum_of_length_are_integers(char_rows):
    assert await CharFields.objects.all().aggregate(low=Min(Length("char")), total=Sum(Length("char"))) == {
        "low": 1,
        "total": 4,
    }


@pytest.mark.asyncio
async def test_avg_of_length_is_a_float(char_rows):
    result = await CharFields.objects.all().aggregate(result=Avg(Length("char")))
    assert result == {"result": 2.0}
    assert isinstance(result["result"], float)


@pytest.mark.asyncio
async def test_coalesce_of_length_is_an_integer(char_rows):
    values = (
        await CharFields.objects.all()
        .annotate(c=Coalesce(Length("char"), 0))
        .order_by("id")
        .values_list("c", flat=True)
    )
    assert values == [3, 1]


@pytest.mark.asyncio
async def test_max_of_f_referencing_length_annotation_is_an_integer(char_rows):
    """F() naming a Length annotation used to take the CharField of Length's own argument."""
    assert await CharFields.objects.all().annotate(size=Length("char")).aggregate(result=Max(F("size"))) == {
        "result": 3
    }
    values = (
        await CharFields.objects.all()
        .annotate(size=Length("char"))
        .annotate(c=Coalesce(F("size"), 0))
        .order_by("id")
        .values_list("c", flat=True)
    )
    assert values == [3, 1]


@pytest.mark.asyncio
async def test_length_arithmetic_and_case_nesting(char_rows):
    result = (
        await CharFields.objects.all()
        .annotate(size=Length("char"))
        .aggregate(
            doubled=Max(F("size") * 2),
            branch=Max(Case(When(char="abc", then=Length("char")), default=0)),
        )
    )
    assert result == {"doubled": 6, "branch": 3}


@pytest.mark.asyncio
async def test_text_functions_nested_in_max_stay_text(char_rows):
    """A text-returning nested function keeps decoding as text."""
    assert await CharFields.objects.all().aggregate(result=Max(Upper(Trim("char")))) == {"result": "ABC"}
    values = (
        await CharFields.objects.all()
        .annotate(c=Coalesce(Lower("char"), "none"))
        .order_by("id")
        .values_list("c", flat=True)
    )
    assert values == ["abc", "a"]


@pytest.mark.asyncio
async def test_max_of_length_of_datetime_on_sqlite(db):
    """LENGTH of a datetime column is its text length on SQLite - used to decode as a 1970
    datetime through the DatetimeField argument."""
    if DatetimeFields._meta.connection.dialect.name != "sqlite":
        pytest.skip("LENGTH(timestamp) doesn't exist on Postgres")
    await DatetimeFields.objects.create(datetime=datetime(2024, 1, 1, tzinfo=UTC))
    result = await DatetimeFields.objects.all().aggregate(result=Max(Length("datetime")))
    assert type(result["result"]) is int


@pytest.mark.asyncio
async def test_function_over_annotation_name_keeps_the_annotation_type(db):
    await DecimalFields.objects.create(decimal=Decimal("1.1000"), decimal_nodec=1)
    await DecimalFields.objects.create(decimal=Decimal("2.2500"), decimal_nodec=2)
    decimals = DecimalFields.objects.annotate(amount=F("decimal"))
    by_name = await decimals.aggregate(total=Sum("amount"))
    assert by_name == await decimals.aggregate(total=Sum(F("amount")))
    assert by_name == {"total": Decimal("3.3500")}
    assert type(by_name["total"]) is Decimal

    await DateFields.objects.create(date=date(2020, 1, 2))
    await DateFields.objects.create(date=date(2021, 1, 2))
    dates = DateFields.objects.annotate(day=F("date"))
    assert await dates.aggregate(latest=Max("day")) == {"latest": date(2021, 1, 2)}
    assert await dates.annotate(day_or_default=Coalesce("day", date(1999, 1, 1))).order_by("id").values_list(
        "day_or_default", flat=True
    ) == [date(2020, 1, 2), date(2021, 1, 2)]
