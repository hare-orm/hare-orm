"""The plan verification of the test suite (``--verify-plans``): every query run on a kept plan - by
its description, by the calls of its queryset, by a direct ``get()`` - is compared with the same
query built in full, and a plan giving another statement is caught."""

from contextlib import contextmanager

import pytest

from hare.query.plans.statement.statement_plans import StatementPlans
from tests.plan_verification.exceptions import PlanMismatchError
from tests.plan_verification.plan_verification import PlanVerification
from tests.testmodels import IntFields


@contextmanager
def plan_verification():
    """Plan verification for the block - installed by the test when the run hasn't."""
    installed_here = not PlanVerification.originals
    if installed_here:
        PlanVerification.install()
    try:
        # The runs are compared when the test asks - verify_pending().
        with PlanVerification.deferring(True):
            yield
    finally:
        PlanVerification.pending.clear()
        if installed_here:
            PlanVerification.uninstall()


@contextmanager
def broken_plans_of(model: type):
    """Gives every plan kept for ``model`` an SQL text other than its full build's - a space more -
    for the block."""
    original_sqls = {
        id(plan): (plan, plan.sql)
        for plan in StatementPlans.plans.values()
        if plan.sql is not None and model._meta.db_table in plan.sql
    }
    for plan, sql in original_sqls.values():
        plan.sql = f"{sql} "
    try:
        yield
    finally:
        for plan, sql in original_sqls.values():
            plan.sql = sql


async def create_numbers() -> None:
    for number in range(1, 6):
        await IntFields.objects.create(id=number, intnum=number, intnum_null=number * 10)


async def run_twice(make_run) -> None:
    """Runs a query twice - the second run is on the plan the first kept."""
    await make_run()
    PlanVerification.verify_pending()
    await make_run()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "make_run",
    [
        pytest.param(lambda: IntFields.objects.filter(intnum__gte=2).count(), id="description plan"),
        pytest.param(lambda: IntFields.objects.filter(intnum__gte=2).order_by("intnum").first(), id="calls plan"),
        pytest.param(lambda: IntFields.objects.get(id=3), id="direct get"),
    ],
)
async def test_a_run_on_a_plan_matching_its_full_build_passes(db, make_run):
    await create_numbers()
    with plan_verification():
        await run_twice(make_run)
        assert PlanVerification.pending
        PlanVerification.verify_pending()
        assert not PlanVerification.pending


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "make_run",
    [
        pytest.param(lambda: IntFields.objects.filter(intnum__gte=2).count(), id="description plan"),
        pytest.param(lambda: IntFields.objects.filter(intnum__gte=2).order_by("intnum").first(), id="calls plan"),
        pytest.param(lambda: IntFields.objects.get(id=3), id="direct get"),
    ],
)
async def test_a_plan_giving_another_statement_is_caught(db, make_run):
    await create_numbers()
    with plan_verification():
        await run_twice(make_run)
        PlanVerification.verify_pending()
        with broken_plans_of(IntFields):
            await make_run()
        mismatch_count = len(PlanVerification.mismatches)
        with pytest.raises(PlanMismatchError, match="ran on a plan giving another statement than its full build"):
            PlanVerification.verify_pending()
        assert len(PlanVerification.mismatches) == mismatch_count + 1
        # The mismatch found on purpose fails no run.
        PlanVerification.mismatches.pop()


@pytest.mark.parametrize(
    ("built_parameter", "planned_parameter", "matches"),
    [
        (1, 1, True),
        (1, 1.0, False),
        ("a", "a", True),
        (float("nan"), float("nan"), True),
        (2, 3, False),
    ],
)
def test_parameters_match_by_value_and_type(built_parameter, planned_parameter, matches):
    assert PlanVerification.parameter_matches(built_parameter, planned_parameter) is matches
