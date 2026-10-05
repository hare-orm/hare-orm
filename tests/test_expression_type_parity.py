"""Annotation results must have the same value and Python type on every backend."""

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio

from hare import Connections
from hare.exceptions import QueryError
from hare.query.expressions import Case, F, OuterReference, Subquery, Value, When, Window
from hare.query.functions import Avg, Coalesce, Concat, Count, Length, Max, Round, Sum
from hare.query.functions.window import Sum as WindowSum
from tests.testmodels import ExpressionTypeChild, ExpressionTypeParity, ExpressionTypeRow

BIG_VALUE = 2**60 + 3
AWARE_DATETIME = datetime(2020, 1, 2, 3, 4, 5, tzinfo=timezone(timedelta(hours=3)))


def assert_same_values(actual: list, expected: list) -> None:
    assert actual == expected
    assert [type(value) for value in actual] == [type(value) for value in expected]
    assert [str(value) for value in actual] == [str(value) for value in expected]


@pytest_asyncio.fixture
async def parity_rows(db):
    await ExpressionTypeParity.objects.create(
        num=1, big=BIG_VALUE, dec=Decimal("1.10"), flag=True, grp="a", ts=datetime(2020, 1, 2, tzinfo=timezone.utc)
    )
    await ExpressionTypeParity.objects.create(
        num=2, big=5, dec=Decimal("0.40"), flag=False, grp="b", ts=datetime(2021, 1, 2, tzinfo=timezone.utc)
    )


async def annotated_values(expression) -> list:
    return await ExpressionTypeParity.objects.all().annotate(c=expression).order_by("num").values_list("c", flat=True)


# -- F4: integer % stays integer ---------------------------------------------------


@pytest.mark.asyncio
async def test_integer_modulo_is_exact_integer(parity_rows):
    assert_same_values(await annotated_values(F("big") % 7), [BIG_VALUE % 7, 5])
    assert_same_values(await annotated_values(F("num") % 2), [1, 0])


@pytest.mark.asyncio
async def test_integer_modulo_filter_order_and_values(parity_rows):
    queryset = ExpressionTypeParity.objects.all().annotate(c=F("big") % 7)
    assert await queryset.filter(c=BIG_VALUE % 7).values_list("num", flat=True) == [1]
    assert await queryset.order_by("-c").values_list("num", "c") == [(2, 5), (1, BIG_VALUE % 7)]
    assert await queryset.order_by("num").values("num", "c") == [
        {"num": 1, "c": BIG_VALUE % 7},
        {"num": 2, "c": 5},
    ]


@pytest.mark.asyncio
async def test_integer_modulo_in_aggregate_and_update(parity_rows):
    assert await ExpressionTypeParity.objects.all().aggregate(total=Sum(F("big") % 7)) == {"total": BIG_VALUE % 7 + 5}
    await ExpressionTypeParity.objects.filter(num=1).update(big=F("big") % 7)
    assert await ExpressionTypeParity.objects.get(num=1).values_list("big", flat=True) == BIG_VALUE % 7


@pytest.mark.asyncio
async def test_decimal_modulo_is_exact_decimal(parity_rows):
    assert_same_values(await annotated_values(F("dec") % 1), [Decimal("0.10"), Decimal("0.40")])
    assert_same_values(await annotated_values(F("num") % Decimal("0.3")), [Decimal("0.1"), Decimal("0.2")])


# -- F5: Decimal arithmetic with a literal ----------------------------------------


@pytest.mark.asyncio
async def test_decimal_field_with_integer_literal(parity_rows):
    assert_same_values(await annotated_values(F("dec") + 1), [Decimal("2.10"), Decimal("1.40")])
    assert_same_values(await annotated_values(F("dec") * 3), [Decimal("3.30"), Decimal("1.20")])


@pytest.mark.asyncio
async def test_decimal_quotient_is_not_rounded(parity_rows):
    """Precision over dialect parity: a Decimal quotient isn't rounded to the operands' scale -
    Postgres keeps its exact numeric result, SQLite (double arithmetic) the nearest Decimal."""
    quotients = await annotated_values(F("dec") / 3)
    exact_quotients = [Decimal("1.10") / 3, Decimal("0.40") / 3]
    assert [type(quotient) for quotient in quotients] == [Decimal, Decimal]
    assert all(abs(quotient - exact) < Decimal("1e-15") for quotient, exact in zip(quotients, exact_quotients))
    if Connections.get("models").dialect.name == "postgresql":
        assert [str(quotient) for quotient in quotients] == ["0.36666666666666666667", "0.13333333333333333333"]


@pytest.mark.asyncio
async def test_decimal_literal_keeps_its_own_scale(parity_rows):
    assert_same_values(await annotated_values(F("dec") + Decimal("1.005")), [Decimal("2.105"), Decimal("1.405")])
    assert_same_values(await annotated_values(F("num") * Decimal("0.5")), [Decimal("0.5"), Decimal("1.0")])
    assert_same_values(await annotated_values(F("dec") * F("dec")), [Decimal("1.2100"), Decimal("0.1600")])


@pytest.mark.asyncio
async def test_decimal_literal_scale_is_part_of_the_cached_shape(parity_rows):
    assert_same_values(await annotated_values(F("dec") + Decimal("1.005")), [Decimal("2.105"), Decimal("1.405")])
    assert_same_values(await annotated_values(F("dec") + Decimal("1.5")), [Decimal("2.60"), Decimal("1.90")])


@pytest.mark.asyncio
async def test_float_literal_still_gives_float(parity_rows):
    values = await annotated_values(F("dec") + 0.5)
    assert values == pytest.approx([1.6, 0.9])
    assert all(type(value) is float for value in values)


@pytest.mark.asyncio
async def test_decimal_arithmetic_filter_values_order_aggregate_update(parity_rows):
    queryset = ExpressionTypeParity.objects.all().annotate(c=F("dec") + 1)
    assert await queryset.filter(c__gt=Decimal("2")).values_list("num", flat=True) == [1]
    assert await queryset.order_by("c").values("c") == [{"c": Decimal("1.40")}, {"c": Decimal("2.10")}]
    assert await ExpressionTypeParity.objects.all().aggregate(top=Max(F("dec") + 1)) == {"top": Decimal("2.10")}
    await ExpressionTypeParity.objects.filter(num=2).update(dec=F("dec") + Decimal("0.05"))
    assert await ExpressionTypeParity.objects.get(num=2).values_list("dec", flat=True) == Decimal("0.45")


# -- F6: Case with bool/Decimal/date literals --------------------------------------


@pytest.mark.asyncio
async def test_case_literal_branches_decode_to_their_type(parity_rows):
    assert_same_values(await annotated_values(Case(When(num=1, then=True), default=False)), [True, False])
    assert_same_values(
        await annotated_values(Case(When(num=1, then=Decimal("1.50")), default=Decimal("0"))),
        [Decimal("1.50"), Decimal("0.00")],
    )
    assert_same_values(
        await annotated_values(Case(When(num=1, then=date(2020, 1, 2)), default=date(2021, 1, 1))),
        [date(2020, 1, 2), date(2021, 1, 1)],
    )
    datetime_values = await annotated_values(Case(When(num=1, then=AWARE_DATETIME), default=None))
    assert datetime_values[0] == AWARE_DATETIME
    assert isinstance(datetime_values[0], datetime)
    assert datetime_values[1] is None


@pytest.mark.asyncio
async def test_case_bool_literal_filter_and_values(parity_rows):
    queryset = ExpressionTypeParity.objects.all().annotate(c=Case(When(num=1, then=True), default=False))
    assert await queryset.filter(c=True).values_list("num", flat=True) == [1]
    assert await queryset.order_by("num").values("num", "c") == [{"num": 1, "c": True}, {"num": 2, "c": False}]


# -- F7: bare Value annotations ---------------------------------------------------


@pytest.mark.asyncio
async def test_bare_value_annotation_types(parity_rows):
    assert_same_values(await annotated_values(Value(True)), [True, True])
    assert_same_values(await annotated_values(Value(Decimal("1.50"))), [Decimal("1.50"), Decimal("1.50")])
    assert_same_values(await annotated_values(Value(date(2020, 1, 2))), [date(2020, 1, 2), date(2020, 1, 2)])
    datetime_values = await annotated_values(Value(AWARE_DATETIME))
    assert datetime_values == [AWARE_DATETIME, AWARE_DATETIME]
    assert all(isinstance(value, datetime) and value.tzinfo is not None for value in datetime_values)


@pytest.mark.asyncio
async def test_bare_value_annotation_with_values_and_model_instances(parity_rows):
    rows = (
        await ExpressionTypeParity.objects.all().annotate(c=Value(Decimal("1.50"))).order_by("num").values("num", "c")
    )
    assert rows == [{"num": 1, "c": Decimal("1.50")}, {"num": 2, "c": Decimal("1.50")}]
    instances = await ExpressionTypeParity.objects.all().annotate(c=Value(date(2020, 1, 2))).order_by("num")
    assert [instance.c for instance in instances] == [date(2020, 1, 2), date(2020, 1, 2)]


# -- F8: Concat with bool/datetime literals ----------------------------------------


@pytest.mark.asyncio
async def test_concat_bool_literal_and_field_render_alike(parity_rows):
    assert_same_values(await annotated_values(Concat("grp", True)), ["atrue", "btrue"])
    assert_same_values(await annotated_values(Concat("grp", F("flag"))), ["atrue", "bfalse"])
    assert_same_values(await annotated_values(Concat("flag", "-", F("grp"))), ["true-a", "false-b"])


@pytest.mark.asyncio
async def test_concat_temporal_literals(parity_rows):
    assert_same_values(
        await annotated_values(Concat("grp", AWARE_DATETIME)),
        ["a2020-01-02 00:04:05+00:00", "b2020-01-02 00:04:05+00:00"],
    )
    assert_same_values(await annotated_values(Concat("grp", date(2020, 1, 2))), ["a2020-01-02", "b2020-01-02"])


@pytest.mark.asyncio
async def test_concat_bool_filter(parity_rows):
    queryset = ExpressionTypeParity.objects.all().annotate(c=Concat("grp", F("flag")))
    assert await queryset.filter(c="bfalse").values_list("num", flat=True) == [2]


# -- Mixed-type expressions over ExpressionTypeRow ---------------------------------


@pytest_asyncio.fixture
async def typed_rows(db):
    await ExpressionTypeRow.objects.create(
        num=3, fl=1.5, dec=Decimal("1.10"), dec3=Decimal("2.125"), d=date(2020, 1, 2),
        dt=datetime(2020, 1, 2, 3, 4, 5, tzinfo=timezone.utc), td=timedelta(hours=1, microseconds=5),
        s="abc", flag=True, num_null=None, dec_null=None,
    )  # fmt: skip
    await ExpressionTypeRow.objects.create(
        num=4, fl=2.25, dec=Decimal("0.40"), dec3=Decimal("1.001"), d=date(2021, 3, 4),
        dt=datetime(2021, 3, 4, 5, 6, 7, tzinfo=timezone.utc), td=timedelta(days=1),
        s="de", flag=False, num_null=5, dec_null=Decimal("3.30"),
    )  # fmt: skip
    await ExpressionTypeRow.objects.create(
        num=10, fl=-3.0, dec=Decimal("2.00"), dec3=Decimal("0.500"), d=date(2022, 5, 6),
        dt=datetime(2022, 5, 6, 7, 8, 9, tzinfo=timezone.utc), td=timedelta(seconds=30),
        s="fghi", flag=True, num_null=7, dec_null=Decimal("1.00"),
    )  # fmt: skip
    rows = await ExpressionTypeRow.objects.all().order_by("num")
    for row, num, dec in ((rows[0], 1, "1.50"), (rows[0], 2, "2.25"), (rows[1], 3, "0.10")):
        await ExpressionTypeChild.objects.create(row=row, num=num, dec=Decimal(dec))
    return rows


async def typed_values(expression) -> list:
    return await ExpressionTypeRow.objects.all().annotate(c=expression).order_by("num").values_list("c", flat=True)


def assert_same_aggregate(actual: dict, expected: dict) -> None:
    assert actual == expected
    assert {key: type(value) for key, value in actual.items()} == {key: type(value) for key, value in expected.items()}
    assert {key: str(value) for key, value in actual.items()} == {key: str(value) for key, value in expected.items()}


@pytest.mark.asyncio
async def test_decimal_division_by_an_integer_is_not_integer_division(typed_rows):
    quotients = await typed_values(F("dec") / 3)
    assert [type(quotient) for quotient in quotients] == [Decimal, Decimal, Decimal]
    exact_quotients = [Decimal("1.10") / 3, Decimal("0.40") / 3, Decimal("2.00") / 3]
    assert all(abs(quotient - exact) < Decimal("1e-15") for quotient, exact in zip(quotients, exact_quotients))
    assert_same_values(await typed_values(Round(F("dec") / 3, 2)), [Decimal("0.37"), Decimal("0.13"), Decimal("0.67")])
    halves = await typed_values(F("num") / Decimal("2"))
    assert halves == [Decimal("1.5"), Decimal("2"), Decimal("5")]
    assert all(type(value) is Decimal for value in halves)


@pytest.mark.asyncio
async def test_decimal_sum_divided_by_count_is_decimal(typed_rows):
    rows = (
        await ExpressionTypeRow.objects.filter(num__in=[4, 10])
        .annotate(c=Sum("dec") / Count("id"))
        .group_by("flag")
        .order_by("flag")
        .values_list("flag", "c")
    )
    assert rows == [(False, Decimal("0.4")), (True, Decimal("2"))]
    assert all(type(value) is Decimal for _flag, value in rows)
    average = await ExpressionTypeRow.objects.all().annotate(third=F("dec") / 3).aggregate(c=Avg("third"))
    assert type(average["c"]) is Decimal
    assert abs(average["c"] - Decimal("3.50") / 9) < Decimal("1e-15")


@pytest.mark.asyncio
async def test_decimal_modulo_is_exact_on_every_backend(typed_rows):
    assert_same_values(await typed_values(F("dec") % Decimal("0.1")), [Decimal("0.00")] * 3)
    assert_same_values(
        await typed_values(F("dec3") % Decimal("0.1")), [Decimal("0.025"), Decimal("0.001"), Decimal("0.000")]
    )
    assert_same_values(await typed_values(2 % F("dec")), [Decimal("0.90"), Decimal("0.00"), Decimal("0.00")])
    queryset = ExpressionTypeRow.objects.annotate(c=F("dec") % Decimal("0.3")).filter(c=Decimal("0.2"))
    assert await queryset.order_by("num").values_list("num", flat=True) == [3, 10]


@pytest.mark.asyncio
async def test_float_modulo_is_a_float_on_every_backend(typed_rows):
    assert_same_values(await typed_values(F("fl") % 2), [1.5, 0.25, -1.0])
    assert_same_values(await typed_values(F("fl") % 1.5), [0.0, 0.75, 0.0])
    assert_same_values(await typed_values(F("num") % 1.5), [0.0, 1.0, 1.0])
    assert_same_values(await typed_values(F("fl") % F("fl")), [0.0, 0.0, 0.0])
    remainders = await typed_values(F("dec") % 0.5)
    assert remainders == pytest.approx([0.1, 0.4, 0.0])
    assert all(type(value) is float for value in remainders)


@pytest.mark.asyncio
async def test_timedelta_modulo_is_an_integer_of_microseconds(typed_rows):
    assert_same_values(await typed_values(F("td") % 2), [1, 0, 0])
    assert_same_values(await typed_values(F("td") % 7), [3600000005 % 7, 86400000000 % 7, 30000000 % 7])


@pytest.mark.asyncio
async def test_count_combined_with_a_decimal_is_decimal(typed_rows):
    assert_same_values(
        await typed_values(Count("children") * F("dec")), [Decimal("2.20"), Decimal("0.40"), Decimal("0.00")]
    )
    assert_same_values(
        await typed_values(Sum("children__dec") * Count("children")), [Decimal("7.50"), Decimal("0.10"), None]
    )
    assert_same_values(
        await typed_values(Round(Count("children") * F("dec"), 1)), [Decimal("2.2"), Decimal("0.4"), Decimal("0.0")]
    )
    instances = await ExpressionTypeRow.objects.all().annotate(c=Count("children") * F("dec")).order_by("num")
    assert_same_values([instance.c for instance in instances], [Decimal("2.20"), Decimal("0.40"), Decimal("0.00")])
    assert_same_aggregate(
        await ExpressionTypeRow.objects.all().aggregate(c=Sum("dec") * Count("id")), {"c": Decimal("10.50")}
    )
    count_times_average = await ExpressionTypeRow.objects.all().aggregate(c=Count("id") * Avg("dec"))
    assert type(count_times_average["c"]) is Decimal
    assert abs(count_times_average["c"] - Decimal("3.50")) < Decimal("1e-15")


@pytest.mark.asyncio
async def test_integer_and_decimal_branches_give_a_decimal(typed_rows):
    assert_same_values(
        await typed_values(Case(When(flag=True, then=F("num")), default=F("dec"))),
        [Decimal("3.00"), Decimal("0.40"), Decimal("10.00")],
    )
    assert_same_values(
        await typed_values(Case(When(flag=True, then=Count("children")), default=F("dec"))),
        [Decimal("2.00"), Decimal("0.40"), Decimal("0.00")],
    )
    assert_same_values(
        await typed_values(Case(When(flag=True, then=F("num")), default=Decimal("0.5"))),
        [Decimal("3.0"), Decimal("0.5"), Decimal("10.0")],
    )
    assert_same_values(
        await typed_values(Case(When(flag=True, then=Length("s")), default=F("dec"))),
        [Decimal("3.00"), Decimal("0.40"), Decimal("4.00")],
    )
    assert_same_values(
        await typed_values(Coalesce("num_null", F("dec"))), [Decimal("1.10"), Decimal("5.00"), Decimal("7.00")]
    )
    assert_same_values(
        await typed_values(Coalesce("num_null", Decimal("0.5"))), [Decimal("0.5"), Decimal("5.0"), Decimal("7.0")]
    )
    assert_same_values(
        await typed_values(Coalesce(Value(None), F("dec"))), [Decimal("1.10"), Decimal("0.40"), Decimal("2.00")]
    )
    assert_same_aggregate(
        await ExpressionTypeRow.objects.all().aggregate(c=Max(Case(When(flag=True, then=F("num")), default=F("dec")))),
        {"c": Decimal("10.00")},
    )


@pytest.mark.asyncio
async def test_integer_and_float_branches_give_a_float(typed_rows):
    assert_same_values(await typed_values(Case(When(flag=True, then=F("num")), default=F("fl"))), [3.0, 2.25, 10.0])
    assert_same_values(await typed_values(Case(When(flag=True, then=F("num")), default=0.5)), [3.0, 0.5, 10.0])
    assert_same_values(await typed_values(Case(When(flag=True, then=1), default=0.5)), [1.0, 0.5, 1.0])
    assert_same_values(await typed_values(Coalesce("num_null", 0.5)), [0.5, 5.0, 7.0])
    assert_same_values(await typed_values(Coalesce("num_null", F("fl"))), [1.5, 5.0, 7.0])
    assert_same_aggregate(
        await ExpressionTypeRow.objects.all().aggregate(c=Max(Case(When(flag=True, then=F("num")), default=0.5))),
        {"c": 10.0},
    )


@pytest.mark.asyncio
async def test_date_and_datetime_literal_branches_give_a_datetime(typed_rows):
    midnight = datetime(2020, 1, 1, tzinfo=timezone.utc)
    values = await typed_values(Case(When(flag=True, then=date(2020, 1, 1)), default=midnight))
    assert values == [midnight] * 3
    assert all(type(value) is datetime and value.utcoffset() == timedelta(0) for value in values)


@pytest.mark.asyncio
async def test_decimal_branches_keep_the_largest_scale(typed_rows):
    assert_same_values(
        await typed_values(Case(When(flag=True, then=F("dec")), default=Decimal("0.125"))),
        [Decimal("1.100"), Decimal("0.125"), Decimal("2.000")],
    )
    assert_same_values(
        await typed_values(Case(When(flag=True, then=F("dec")), default=F("dec3"))),
        [Decimal("1.100"), Decimal("1.001"), Decimal("2.000")],
    )
    assert_same_values(
        await typed_values(Coalesce("dec_null", Decimal("0.125"))),
        [Decimal("0.125"), Decimal("3.300"), Decimal("1.000")],
    )
    assert_same_values(
        await typed_values(Coalesce("dec_null", F("dec3"))), [Decimal("2.125"), Decimal("3.300"), Decimal("1.000")]
    )


@pytest.mark.asyncio
async def test_decimal_and_float_branches_give_a_float(typed_rows):
    assert_same_values(await typed_values(Case(When(flag=True, then=F("dec")), default=0.125)), [1.1, 0.125, 2.0])
    assert_same_values(await typed_values(Coalesce("dec_null", 0.125)), [0.125, 3.3, 1.0])


@pytest.mark.asyncio
async def test_aggregates_of_untyped_integers(typed_rows):
    assert_same_aggregate(await ExpressionTypeRow.objects.all().aggregate(c=Sum(Value(1))), {"c": 3})
    assert_same_aggregate(await ExpressionTypeRow.objects.all().aggregate(c=Avg(Value(2))), {"c": 2.0})
    assert_same_aggregate(await ExpressionTypeRow.objects.all().aggregate(c=Sum(F("td") * 2)), {"c": 180060000010})
    assert_same_aggregate(
        await ExpressionTypeRow.objects.all().aggregate(c=Sum("td") / Count("id")), {"c": 30010000001}
    )
    assert_same_aggregate(await ExpressionTypeRow.objects.all().aggregate(c=Count("id") * Avg("num")), {"c": 17.0})
    doubled_average = await ExpressionTypeRow.objects.all().aggregate(c=Avg("num") * Decimal("2"))
    assert doubled_average["c"] == pytest.approx(34 / 3)
    assert type(doubled_average["c"]) is float
    assert_same_aggregate(
        await ExpressionTypeRow.objects.all().annotate(y=Value(Decimal("1.5"))).aggregate(c=Sum("y")),
        {"c": Decimal("4.5")},
    )


@pytest.mark.asyncio
async def test_window_sum_over_literal_and_timedelta_annotations(typed_rows):
    def windowed(annotation):
        return (
            ExpressionTypeRow.objects.all()
            .annotate(y=annotation)
            .annotate(c=Window(WindowSum("y"), order_by=["num"]))
            .order_by("num")
            .values_list("c", flat=True)
        )

    assert_same_values(await windowed(F("td") * 2), [7200000010, 180000000010, 180060000010])
    assert_same_values(await windowed(Value(1)), [1, 2, 3])
    assert_same_values(await windowed(Value(Decimal("1.5"))), [Decimal("1.5"), Decimal("3.0"), Decimal("4.5")])


@pytest.mark.asyncio
async def test_concat_of_arithmetic_and_numbers_renders_alike(typed_rows):
    assert_same_values(await typed_values(Concat("s", F("dec") * 2)), ["abc2.20", "de0.80", "fghi4.00"])
    assert_same_values(await typed_values(Concat("s", F("num") / 2)), ["abc1", "de2", "fghi5"])
    assert_same_values(await typed_values(Concat("s", F("fl") * 1)), ["abc1.5", "de2.25", "fghi-3"])
    assert_same_values(await typed_values(Concat("s", F("dec"))), ["abc1.10", "de0.40", "fghi2.00"])
    assert_same_values(await typed_values(Concat("s", F("fl"))), ["abc1.5", "de2.25", "fghi-3"])
    assert_same_values(await typed_values(Concat("s", 2.0)), ["abc2", "de2", "fghi2"])
    assert_same_values(
        await typed_values(Concat("s", F("dt"))),
        ["abc2020-01-02 03:04:05+00:00", "de2021-03-04 05:06:07+00:00", "fghi2022-05-06 07:08:09+00:00"],
    )
    assert_same_values(await typed_values(Concat("s", Avg("children__num"))), ["abc1.5", "de3", "fghi"])
    assert_same_values(await typed_values(Concat("s", Sum("children__dec"))), ["abc3.75", "de0.10", "fghi"])


@pytest.mark.asyncio
async def test_concat_float_text_matches_postgres(typed_rows):
    assert_same_values(
        await typed_values(Concat("s", "|", 1e15, "|", 1e-05, "|", 0.1 + 0.2, "|", 1 / 3, "|", 123456789012345.0)),
        [
            f"{prefix}|1e+15|1e-05|0.30000000000000004|0.3333333333333333|123456789012345"
            for prefix in ("abc", "de", "fghi")
        ],
    )
    assert_same_values(await typed_values(Concat("s", F("fl") / 3)), ["abc0.5", "de0.75", "fghi-1"])
    assert_same_values(await typed_values(Concat("s", F("fl") * 1e15)), ["abc1.5e+15", "de2.25e+15", "fghi-3e+15"])


def test_concat_rejects_a_bytes_literal():
    with pytest.raises(QueryError, match="bytes"):
        Concat("s", b"x")
    with pytest.raises(QueryError, match="bytes"):
        Concat("s", Value(b"x"))


@pytest.mark.asyncio
async def test_timedelta_uuid_dict_and_bytes_literals(typed_rows):
    hour = timedelta(hours=1)
    identifier = uuid.UUID(int=1)
    assert_same_values(
        await typed_values(Coalesce("td", timedelta(0))),
        [timedelta(hours=1, microseconds=5), timedelta(days=1), timedelta(seconds=30)],
    )
    assert_same_values(
        await typed_values(Case(When(flag=True, then=F("td")), default=timedelta(0))),
        [timedelta(hours=1, microseconds=5), timedelta(0), timedelta(seconds=30)],
    )
    assert_same_values(await typed_values(Case(When(flag=True, then=hour))), [hour, None, hour])
    assert_same_values(await typed_values(Value(hour)), [hour] * 3)
    assert_same_values(await typed_values(Concat("s", hour)), ["abc3600000000", "de3600000000", "fghi3600000000"])
    assert_same_values(await typed_values(Case(When(flag=True, then=identifier))), [identifier, None, identifier])
    assert_same_values(await typed_values(Value(identifier)), [identifier] * 3)
    assert_same_values(
        await typed_values(Concat("s", identifier)), [f"{prefix}{identifier}" for prefix in ("abc", "de", "fghi")]
    )
    assert_same_values(await typed_values(Value(b"x")), [b"x"] * 3)
    assert await typed_values(Value({"a": 1})) == [{"a": 1}] * 3


@pytest.mark.asyncio
async def test_encoded_literals_are_rebound_by_the_cached_query_shape(typed_rows):
    for hours in (1, 2):
        assert_same_values(
            await typed_values(Case(When(flag=False, then=timedelta(hours=hours)), default=F("td"))),
            [timedelta(hours=1, microseconds=5), timedelta(hours=hours), timedelta(seconds=30)],
        )
        assert_same_values(await typed_values(Value(uuid.UUID(int=hours))), [uuid.UUID(int=hours)] * 3)
        assert_same_values(
            await typed_values(Case(When(flag=True, then=F("td")), default=timedelta(hours=hours))),
            [timedelta(hours=1, microseconds=5), timedelta(hours=hours), timedelta(seconds=30)],
        )
    for default in (Decimal("0.125"), Decimal("0.250")):
        assert_same_values(
            await typed_values(Coalesce("dec_null", default)), [default, Decimal("3.300"), Decimal("1.000")]
        )


@pytest.mark.asyncio
async def test_subquery_of_a_related_field_decodes_through_that_field(typed_rows):
    def first_child_value(path):
        return Subquery(
            ExpressionTypeChild.objects.filter(row_id=OuterReference("id")).order_by("id").limit(1).values(path)
        )

    assert_same_values(await typed_values(first_child_value("row__dec")), [Decimal("1.10"), Decimal("0.40"), None])
    assert_same_values(
        await typed_values(first_child_value("row__dt")),
        [datetime(2020, 1, 2, 3, 4, 5, tzinfo=timezone.utc), datetime(2021, 3, 4, 5, 6, 7, tzinfo=timezone.utc), None],
    )
    assert_same_values(await typed_values(first_child_value("row__d")), [date(2020, 1, 2), date(2021, 3, 4), None])
    assert_same_values(await typed_values(first_child_value("row__flag")), [True, False, None])
    assert_same_values(
        await typed_values(first_child_value("row__td")), [timedelta(hours=1, microseconds=5), timedelta(days=1), None]
    )
