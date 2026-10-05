import pytest

from hare.exceptions import QueryError
from tests.testmodels import Event, Reporter, Team, Tournament


@pytest.mark.asyncio
async def test_filter_by_an_unsaved_related_instance_raises(db):
    """An unsaved related instance has no key to compare - read as ``reporter_id IS NULL`` it would
    match every event without a reporter, so it raises as ``reporter__in``/``reporter__not`` do."""
    tournament = await Tournament.objects.create(name="Cup")
    await Event.objects.create(name="Final", tournament=tournament)

    with pytest.raises(QueryError, match="got an unsaved Reporter instance"):
        await Event.objects.filter(reporter=Reporter(name="Ann")).count()


@pytest.mark.asyncio
async def test_filter_by_an_instance_of_another_model_raises(db):
    """An instance of another model isn't a value of the relation - its primary key would be
    compared with the relation's key column as if it were one."""
    await Tournament.objects.create(name="Cup")
    team = await Team.objects.create(name="Blue")

    with pytest.raises(QueryError, match="expects Tournament instances or key values, got a Team instance"):
        await Event.objects.filter(tournament=team).count()


@pytest.mark.asyncio
async def test_filter_by_a_saved_related_instance_compares_its_key(db):
    tournament = await Tournament.objects.create(name="Cup")
    other = await Tournament.objects.create(name="Shield")
    await Event.objects.create(name="Final", tournament=tournament)
    await Event.objects.create(name="Semi", tournament=other)

    assert await Event.objects.filter(tournament=tournament).values_list("name", flat=True) == ["Final"]
