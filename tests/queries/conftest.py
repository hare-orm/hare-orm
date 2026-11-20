import pytest_asyncio

from tests.shared_models.testmodels import Author, Book, CharFields, Event, IntFields, Team, Tournament


@pytest_asyncio.fixture
async def library(db):
    first_author = await Author.objects.create(name="First")
    second_author = await Author.objects.create(name="Second")
    third_author = await Author.objects.create(name="Third")
    await Book.objects.create(name="A", author=first_author, rating=1.0)
    await Book.objects.create(name="B", author=first_author, rating=2.0)
    await Book.objects.create(name="C", author=second_author, rating=5.0)
    return first_author, second_author, third_author


@pytest_asyncio.fixture
async def event_with_participants(db):
    tournament = await Tournament.objects.create(name="Tournament A", desc="Description A")
    event = await Event.objects.create(name="Event 1", tournament=tournament)
    team_a = await Team.objects.create(name="Team A")
    team_b = await Team.objects.create(name="Team B")
    await event.participants.add(team_a, team_b)
    return event, team_a, team_b


@pytest_asyncio.fixture
async def tournament_with_events(db):
    tournament = await Tournament.objects.create(name="Tournament A", desc="Description A")
    event1 = await Event.objects.create(name="Event 1", tournament=tournament)
    event2 = await Event.objects.create(name="Event 2", tournament=tournament)
    return tournament, event1, event2


@pytest_asyncio.fixture
async def scores_data(db):
    """intnum 1..5 with intnum_null 1, NULL, 2, 3, NULL."""
    for number, score in [(1, 1), (2, None), (3, 2), (4, 3), (5, None)]:
        await IntFields.objects.create(intnum=number, intnum_null=score)


@pytest_asyncio.fixture
async def char_fields_data(db):
    await CharFields.objects.create(char="moo")
    await CharFields.objects.create(char="baa", char_null="baa")
    await CharFields.objects.create(char="oink")
