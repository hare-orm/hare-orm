import pytest

from hare.contrib.test import requires_features
from hare.exceptions import OperationalError
from tests.testmodels import Employee, Event, Tournament


@pytest.mark.asyncio
async def test_order_by_nested_basic(db):
    await Event.objects.create(
        name="Event 1", tournament=await Tournament.objects.create(name="Tournament 1", desc="B")
    )
    await Event.objects.create(
        name="Event 2", tournament=await Tournament.objects.create(name="Tournament 2", desc="A")
    )

    assert await Event.objects.all().order_by("-name").values("name") == [
        {"name": "Event 2"},
        {"name": "Event 1"},
    ]

    assert await Event.objects.all().values("tournament__desc") == [
        {"tournament__desc": "B"},
        {"tournament__desc": "A"},
    ]

    assert (await Event.objects.all().order_by("tournament__desc").values("tournament__desc")) == [
        {"tournament__desc": "A"},
        {"tournament__desc": "B"},
    ]


@pytest.mark.asyncio
@requires_features(dialect="sqlite")
async def test_a_query_joining_too_many_tables_raises_an_operational_error_naming_the_cause(db):
    too_long_manager_path = "__".join(["manager"] * 70) + "__name"

    with pytest.raises(OperationalError, match="more tables than SQLite allows") as exc_info:
        await Employee.objects.all().order_by(too_long_manager_path)

    assert "at most 64 tables in a join" in str(exc_info.value)
