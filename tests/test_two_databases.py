import os

import pytest
import pytest_asyncio

from hare import prefetch_related_objects
from hare.contrib.test import requires_features
from hare.exceptions import OperationalError, QueryError
from hare.transactions.transactions import Transactions
from tests.testmodels import Event, EventTwo, TeamTwo, Tournament
from tests.utils.multi_database_context import MultiDatabaseTestContext


@pytest_asyncio.fixture(scope="function")
async def two_databases():
    """Fixture that sets up two separate databases for testing."""
    db_url = os.getenv("HARE_TEST_DB", "sqlite://:memory:")
    async with MultiDatabaseTestContext.open(
        db_url,
        ["models", "events"],
        apps={
            "models": {"models": ["tests.testmodels"], "default_connection": "models"},
            "events": {"models": ["tests.testmodels"], "default_connection": "events"},
        },
    ) as ctx:
        await ctx.generate_schemas()

        db = ctx.connections.get("models")
        second_db = ctx.connections.get("events")

        yield db, second_db


@pytest.mark.asyncio
async def test_two_databases(two_databases):
    db, second_db = two_databases

    tournament = await Tournament.objects.create(name="Tournament")
    await EventTwo.objects.create(name="Event", tournament_id=tournament.id)

    select_sql = "SELECT * FROM eventtwo"
    with pytest.raises(OperationalError):
        await db.execute(select_sql)
    _, results = await second_db.execute(select_sql)
    assert dict(results[0]) == {"id": 1, "name": "Event", "tournament_id": 1}


@pytest.mark.asyncio
async def test_two_databases_relation(two_databases):
    db, second_db = two_databases

    tournament = await Tournament.objects.create(name="Tournament")
    event = await EventTwo.objects.create(name="Event", tournament_id=tournament.id)

    select_sql = "SELECT * FROM eventtwo"
    with pytest.raises(OperationalError):
        await db.execute(select_sql)

    _, results = await second_db.execute(select_sql)
    assert dict(results[0]) == {"id": 1, "name": "Event", "tournament_id": 1}

    teams = []
    for i in range(2):
        team = await TeamTwo.objects.create(name=f"Team {(i + 1)}")
        teams.append(team)
        await event.participants.add(team)

    assert await TeamTwo.objects.all().order_by("name") == teams
    assert await event.participants.all().order_by("name") == teams

    assert await TeamTwo.objects.all().order_by("name").values("id", "name") == [
        {"id": 1, "name": "Team 1"},
        {"id": 2, "name": "Team 2"},
    ]
    assert await event.participants.all().order_by("name").values("id", "name") == [
        {"id": 1, "name": "Team 1"},
        {"id": 2, "name": "Team 2"},
    ]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_two_databases_transactions_switch_db(two_databases):
    async with Transactions.atomic("models"):
        tournament = await Tournament.objects.create(name="Tournament")
        await Event.objects.create(name="Event1", tournament=tournament)
        async with Transactions.atomic("events"):
            event = await EventTwo.objects.create(name="Event2", tournament_id=tournament.id)
            team = await TeamTwo.objects.create(name="Team 1")
            await event.participants.add(team)

    saved_tournament = await Tournament.objects.filter(name="Tournament").first()
    assert tournament.id == saved_tournament.id
    saved_event = await EventTwo.objects.filter(tournament_id=tournament.id).first()
    assert event.id == saved_event.id


@pytest.mark.asyncio
async def test_two_databases_transaction_paramerror(two_databases):
    with pytest.raises(
        QueryError,
        match="You are running with multiple databases, so you should specify using",
    ):
        async with Transactions.atomic():
            pass


@pytest_asyncio.fixture(scope="function")
async def default_and_replica():
    """Two connections holding the same schema; every model defaults to "models"."""
    db_url = os.getenv("HARE_TEST_DB", "sqlite://:memory:")
    async with MultiDatabaseTestContext.open(
        db_url,
        ["models", "replica"],
        apps={"models": {"models": ["tests.testmodels"], "default_connection": "models"}},
    ) as ctx:
        await ctx.generate_schemas()
        default_db = ctx.connections.get("models")
        replica_db = ctx.connections.get("replica")
        await replica_db.execute_script(default_db.get_schema_sql(safe=True))
        # Same primary keys on both connections, different names - a lookup on the wrong
        # connection returns the wrong row instead of failing.
        default_tournament = await Tournament.objects.create(name="default tournament")
        await Event.objects.create(name="default event", tournament=default_tournament)
        replica_tournament = await Tournament.objects.using(replica_db).create(name="replica tournament")
        await Event.objects.using(replica_db).create(name="replica event", tournament=replica_tournament)
        yield default_db, replica_db


@pytest.mark.asyncio
async def test_instance_relations_use_the_connection_it_was_loaded_from(default_and_replica):
    event = await Event.objects.all().using("replica").get(name="replica event")

    assert (await event.tournament).name == "replica tournament"
    await prefetch_related_objects([event], "tournament")
    assert event.tournament.name == "replica tournament"

    tournament = await Tournament.objects.all().using("replica").get(name="replica tournament")
    assert [related.name for related in await tournament.events.all()] == ["replica event"]
    assert await tournament.events.all().count() == 1
    assert [row["name"] for row in await tournament.events.all().values("name")] == ["replica event"]


@pytest.mark.asyncio
async def test_select_related_and_prefetched_instances_remember_their_connection(default_and_replica):
    event = await Event.objects.all().using("replica").select_related("tournament").get(name="replica event")
    assert [related.name for related in await event.tournament.events.all()] == ["replica event"]

    tournament = (
        await Tournament.objects.all().using("replica").prefetch_related("events").get(name="replica tournament")
    )
    prefetched_event = tournament.events[0]
    assert (await prefetched_event.tournament).name == "replica tournament"


@pytest.mark.asyncio
async def test_save_refresh_and_delete_use_the_connection_it_was_loaded_from(default_and_replica):
    default_db, replica_db = default_and_replica
    event = await Event.objects.all().using("replica").get(name="replica event")

    event.name = "renamed on replica"
    await event.save()
    assert await Event.objects.all().using(replica_db).filter(name="renamed on replica").count() == 1
    assert await Event.objects.all().using(default_db).filter(name="default event").count() == 1

    await Event.objects.all().using(replica_db).filter(event_id=event.event_id).update(name="changed behind")
    await event.refresh_from_db()
    assert event.name == "changed behind"

    await event.delete()
    assert await Event.objects.all().using(replica_db).count() == 0
    assert await Event.objects.all().using(default_db).count() == 1


@pytest.mark.asyncio
async def test_created_instances_remember_their_connection(default_and_replica):
    _, replica_db = default_and_replica
    tournament = await Tournament.objects.using(replica_db).create(name="created on replica")
    await Event.objects.using(replica_db).create(name="created event", tournament=tournament)
    assert [related.name for related in await tournament.events.all()] == ["created event"]

    fetched, created = await Tournament.objects.using(replica_db).get_or_create(name="created on replica")
    assert not created
    assert [related.name for related in await fetched.events.all()] == ["created event"]

    bulk_tournament = Tournament(id=50, name="bulk on replica")
    await Tournament.objects.using(replica_db).bulk_create([bulk_tournament])
    await Event.objects.using(replica_db).create(name="bulk event", tournament=bulk_tournament)
    assert [related.name for related in await bulk_tournament.events.all()] == ["bulk event"]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_instance_connection_uses_open_transaction_and_yields_to_explicit_connection(default_and_replica):
    default_db, _ = default_and_replica
    tournament = await Tournament.objects.all().using("replica").get(name="replica tournament")

    async with Transactions.atomic("replica") as replica_transaction:
        await Event.objects.using(replica_transaction).create(name="uncommitted event", tournament=tournament)
        names = [related.name for related in await tournament.events.all().order_by("name")]
        assert names == ["replica event", "uncommitted event"]

    await tournament.refresh_from_db(using=default_db)
    assert tournament.name == "default tournament"
