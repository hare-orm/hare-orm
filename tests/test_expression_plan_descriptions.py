"""Each expression describes its own plan (``Plannable.get_plan_description()``): a later query of
the structure it describes runs on the plan the first one kept, its own values bound. Each test
compares a plan hit with the same query built in full."""

from unittest.mock import patch

import pytest

from hare.query.expressions import (
    Case,
    Expression,
    ExpressionContext,
    ExpressionResult,
    F,
    Q,
    Subquery,
    Value,
    When,
    Window,
)
from hare.query.functions import Now, Pi, Sum as AggregateSum
from hare.query.functions.window import CumeDist, Lag, NthValue, NTile, PercentRank, RowNumber, Sum
from hare.query.plans.description import PlanContext, PlanDescription, Plannable
from hare.query.plans.statement_plans import StatementPlans
from hare.query.statements.awaitable_query import AwaitableQuery
from tests.testmodels import IntFields


def full_build():
    """Runs a query built in full, as it ran before plans."""
    return patch.object(AwaitableQuery, "_run_on_plan", lambda self, *args, **kwargs: False)


async def create_numbers() -> None:
    for intnum in (1, 2, 3, 4):
        await IntFields.objects.create(intnum=intnum)


def ordered_rows(queryset, *names):
    return queryset.order_by("intnum").values_list("intnum", *names)


SHAPES = [
    (
        "cumulative distribution",
        lambda value: ordered_rows(
            IntFields.objects.filter(intnum__gte=value).annotate(share=Window(CumeDist(), order_by=["intnum"])),
            "share",
        ),
        (1, 2),
    ),
    (
        "percent rank",
        lambda value: ordered_rows(
            IntFields.objects.filter(intnum__gte=value).annotate(rank=Window(PercentRank(), order_by=["intnum"])),
            "rank",
        ),
        (1, 2),
    ),
    (
        "nth value",
        lambda value: ordered_rows(
            IntFields.objects.filter(intnum__gte=value).annotate(
                second=Window(NthValue("intnum", 2), order_by=["intnum"])
            ),
            "second",
        ),
        (1, 2),
    ),
    (
        "window over an expression holding a literal",
        lambda value: ordered_rows(IntFields.objects.annotate(total=Window(Sum(F("intnum") * value))), "total"),
        (2, 3),
    ),
    (
        "window aggregate with a condition",
        lambda value: ordered_rows(
            IntFields.objects.annotate(total=Window(AggregateSum("intnum", _filter=Q(intnum__gte=value)))), "total"
        ),
        (2, 3),
    ),
    (
        "lag of an annotation holding a literal",
        lambda value: ordered_rows(
            IntFields.objects.annotate(shifted=F("intnum") + value).annotate(
                previous=Window(Lag("shifted", 1, 0), order_by=["intnum"])
            ),
            "previous",
        ),
        (10, 20),
    ),
    (
        "partition by an annotation holding a literal",
        lambda value: ordered_rows(
            IntFields.objects.annotate(bucket=Case(When(intnum__gte=value, then=1), default=0)).annotate(
                position=Window(RowNumber(), partition_by=["bucket"], order_by=["intnum"])
            ),
            "position",
        ),
        (2, 3),
    ),
    (
        "ordered by an unselected annotation holding a literal",
        lambda value: (
            IntFields.objects.annotate(rank=Case(When(intnum=value, then=0), default=1))
            .order_by("rank", "intnum")
            .values_list("intnum", flat=True)
        ),
        (2, 3),
    ),
    (
        "ordered by an alias holding a literal",
        lambda value: (
            IntFields.objects.all()
            .alias(rank=Case(When(intnum=value, then=0), default=1))
            .order_by("rank", "intnum")
            .values_list("intnum", flat=True)
        ),
        (2, 3),
    ),
    (
        "bare value annotation",
        lambda value: ordered_rows(IntFields.objects.annotate(constant=Value(value)), "constant"),
        (7, 8),
    ),
    (
        "subquery annotation",
        lambda value: ordered_rows(
            IntFields.objects.annotate(
                smallest_above=Subquery(
                    IntFields.objects.filter(intnum__gt=value).order_by("intnum").values_list("intnum", flat=True)[:1]
                )
            ),
            "smallest_above",
        ),
        (1, 2),
    ),
    (
        "constant expression",
        lambda value: ordered_rows(IntFields.objects.filter(intnum__gte=value).annotate(pi=Pi()), "pi"),
        (1, 2),
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "build", "values"), SHAPES, ids=[shape[0] for shape in SHAPES])
async def test_a_later_query_of_the_structure_runs_on_its_plan(db, name, build, values):
    await create_numbers()
    first_value, second_value = values
    first = await build(first_value)
    with full_build():
        expected = await build(second_value)
    assert expected != first
    hits = StatementPlans.hits
    assert await build(second_value) == expected
    assert StatementPlans.hits - hits == 1


@pytest.mark.asyncio
async def test_a_query_reading_the_current_moment_runs_on_its_plan(db):
    await create_numbers()
    await IntFields.objects.filter(intnum__gte=1).annotate(now=Now()).values_list("now", flat=True)
    hits = StatementPlans.hits
    assert len(await IntFields.objects.filter(intnum__gte=3).annotate(now=Now()).values_list("now", flat=True)) == 2
    assert StatementPlans.hits - hits == 1


def test_an_expression_argument_is_part_of_the_structure():
    first = Window(NTile(F("intnum"))).get_plan_description(PlanContext.EMPTY)
    second = Window(NTile(F("intnum_null"))).get_plan_description(PlanContext.EMPTY)
    assert first is not None and second is not None
    assert first.structure != second.structure


def test_a_literal_argument_is_bound_by_its_type():
    first = Window(Lag("intnum", 1, 5)).get_plan_description(PlanContext.EMPTY)
    second = Window(Lag("intnum", 2, 7)).get_plan_description(PlanContext.EMPTY)
    third = Window(Lag("intnum", 1, 5.5)).get_plan_description(PlanContext.EMPTY)
    assert first is not None and second is not None and third is not None
    assert (first.structure, first.values, second.values) == (second.structure, [1, 5], [2, 7])
    assert third.structure != first.structure


def test_a_none_default_keeps_no_plan():
    # LAG(..., NULL) renders no parameter where a default value would.
    assert Window(Lag("intnum")).get_plan_description(PlanContext.EMPTY) is None


def test_an_annotation_named_gives_its_values_again():
    annotations = {"shifted": F("intnum") + 10}
    description = F("shifted").get_plan_description(PlanContext(annotations))
    assert description == PlanDescription((F, "shifted"), [10])


def test_a_class_neither_describing_its_plan_nor_declaring_none_is_rejected():
    with pytest.raises(TypeError, match="neither describes its plan"):

        class Undescribed(Expression):
            def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
                raise NotImplementedError

    class Unplanned(Expression):
        plannable = False

    assert Unplanned().get_plan_description(PlanContext.EMPTY) is None


def test_a_class_declaring_no_plan_cannot_describe_one():
    with pytest.raises(TypeError, match="declares plannable = False but describes a plan"):

        class Contradicting(Expression):
            plannable = False

            def get_plan_description(self, context: PlanContext) -> PlanDescription:
                return PlanDescription((), [])


def test_an_abstract_base_describes_through_its_subclasses():
    class Base(Plannable, abstract=True):
        pass

    with pytest.raises(TypeError, match="neither describes its plan"):

        class Concrete(Base):
            pass
