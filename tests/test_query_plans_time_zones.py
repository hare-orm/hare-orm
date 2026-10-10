"""A query renders the zone of its time zone mode into its SQL text - the configured zone under
``use_timezone``, the machine's own without it - and keeps its plan under that zone."""

from datetime import UTC, datetime

import pytest

from hare.query.plans.statement.statement_plans import StatementPlans
from hare.time import Timezone
from tests import testmodels
from tests.utils.timezone_context import override_timezone


async def count_plan_hits(statement) -> tuple[object, int]:
    hits = StatementPlans.hits
    result = await statement
    return result, StatementPlans.hits - hits


@pytest.mark.asyncio
async def test_query_without_time_zone_support_runs_on_its_plan(db):
    model = testmodels.DatetimeFields
    with override_timezone(use_timezone=False):
        try:
            morning = await model.objects.create(datetime=datetime(2024, 1, 15, 5, 0))
            evening = await model.objects.create(datetime=datetime(2024, 7, 15, 17, 0))
            assert await model.objects.filter(id=morning.id, datetime__hour=5).count() == 1
            count, hits = await count_plan_hits(model.objects.filter(id=evening.id, datetime__hour=17).count())
            assert count == 1
            assert hits == 1
            await model.objects.filter(datetime__gte=datetime(2024, 6, 1)).order_by("id")
            rows, hits = await count_plan_hits(model.objects.filter(datetime__gte=datetime(2024, 1, 1)).order_by("id"))
            assert [row.id for row in rows] == [morning.id, evening.id]
            assert hits == 1
        finally:
            await model.objects.all().delete()


@pytest.mark.asyncio
async def test_plans_of_the_two_time_zone_modes_stay_apart(db):
    """The zone a date-part lookup extracts in is part of the key - a plan built in one mode or
    zone is never run in another."""
    model = testmodels.DatetimeFields
    with override_timezone(use_timezone=True, timezone="Europe/Berlin"):
        try:
            obj = await model.objects.create(datetime=datetime(2024, 1, 1, 23, 30, tzinfo=UTC))
            # 00:30 on 2 January in Berlin.
            assert await model.objects.filter(id=obj.id, datetime__day=2).count() == 1
            with override_timezone(use_timezone=True, timezone="UTC"):
                count, hits = await count_plan_hits(model.objects.filter(id=obj.id, datetime__day=2).count())
                assert (count, hits) == (0, 0)
            assert Timezone.get_rendered_zone_name() == "Europe/Berlin"
        finally:
            await model.objects.all().delete()
