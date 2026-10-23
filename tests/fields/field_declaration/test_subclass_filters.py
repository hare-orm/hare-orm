import pytest
import pytest_asyncio

from tests.fields.field_declaration.subclass_models import RaceParticipant, RacePlacingEnum


@pytest_asyncio.fixture
async def race_data(db_subclass_fields):
    """Set up race participant data for filter tests."""
    await RaceParticipant.objects.create(
        first_name="George", place=RacePlacingEnum.FIRST, predicted_place=RacePlacingEnum.SECOND
    )
    await RaceParticipant.objects.create(
        first_name="John", place=RacePlacingEnum.SECOND, predicted_place=RacePlacingEnum.THIRD
    )
    await RaceParticipant.objects.create(first_name="Paul", place=RacePlacingEnum.THIRD)
    await RaceParticipant.objects.create(first_name="Ringo", place=RacePlacingEnum.RUNNER_UP)
    await RaceParticipant.objects.create(first_name="Stuart", predicted_place=RacePlacingEnum.FIRST)
    yield db_subclass_fields


@pytest.mark.asyncio
async def test_equal(race_data):
    """Test equal filter on custom enum field."""
    assert set(await RaceParticipant.objects.filter(place=RacePlacingEnum.FIRST).values_list("place", flat=True)) == {
        RacePlacingEnum.FIRST
    }


@pytest.mark.asyncio
async def test_not(race_data):
    """Test not filter on custom enum field."""
    assert set(
        await RaceParticipant.objects.filter(place__not=RacePlacingEnum.FIRST).values_list("place", flat=True)
    ) == {
        RacePlacingEnum.SECOND,
        RacePlacingEnum.THIRD,
        RacePlacingEnum.RUNNER_UP,
        RacePlacingEnum.DNF,
    }


@pytest.mark.asyncio
async def test_in(race_data):
    """Test in filter on custom enum field."""
    assert set(
        await RaceParticipant.objects.filter(place__in=[RacePlacingEnum.DNF, RacePlacingEnum.RUNNER_UP]).values_list(
            "place", flat=True
        )
    ) == {RacePlacingEnum.DNF, RacePlacingEnum.RUNNER_UP}


@pytest.mark.asyncio
async def test_not_in(race_data):
    """Test not_in filter on custom enum field."""
    assert set(
        await RaceParticipant.objects.filter(
            place__not_in=[RacePlacingEnum.DNF, RacePlacingEnum.RUNNER_UP]
        ).values_list("place", flat=True)
    ) == {RacePlacingEnum.FIRST, RacePlacingEnum.SECOND, RacePlacingEnum.THIRD}


@pytest.mark.parametrize(
    ("lookup", "value_for_missing"),
    [pytest.param("isnull", True, id="isnull"), pytest.param("not_isnull", False, id="not_isnull")],
)
@pytest.mark.asyncio
async def test_isnull(race_data, lookup, value_for_missing):
    """isnull/not_isnull filter on a custom enum field."""
    for value, expected in (
        (value_for_missing, {"Paul", "Ringo"}),
        (not value_for_missing, {"George", "John", "Stuart"}),
    ):
        assert (
            set(
                await RaceParticipant.objects.filter(**{f"predicted_place__{lookup}": value}).values_list(
                    "first_name", flat=True
                )
            )
            == expected
        )
