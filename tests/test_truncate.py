"""
Tests for truncate_all_models() and topological_sort_models().

Verifies FK-aware truncation ordering and the PostgreSQL TRUNCATE CASCADE path.
"""

import pytest

from hare import Hare
from hare.contrib.test import requires_features, topological_sort_models, truncate_all_models
from tests.test_two_databases import two_databases  # noqa: F401 - used as a fixture by parameter name
from tests.testmodels import Employee, Event, EventTwo, MinRelation, Reporter, Team, Tournament

# ---------------------------------------------------------------------------
# topological_sort_models — unit tests on real model metadata
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_topological_sort_children_before_parents(db):
    """Event (FK→Tournament) must come before Tournament in delete order."""
    sorted_models = topological_sort_models([Tournament, Event])
    assert sorted_models.index(Event) < sorted_models.index(Tournament)


@pytest.mark.asyncio
async def test_topological_sort_input_order_independent(db):
    """Result must be the same regardless of input order."""
    order_a = topological_sort_models([Tournament, Event])
    order_b = topological_sort_models([Event, Tournament])
    assert order_a == order_b


@pytest.mark.asyncio
async def test_topological_sort_self_referential_fk(db):
    """Self-referential FK (Employee→Employee) must not cause infinite loop."""
    result = topological_sort_models([Employee])
    assert result == [Employee]


@pytest.mark.asyncio
async def test_topological_sort_no_fk_models(db):
    """Models without FK relationships are still included."""
    result = topological_sort_models([Team])
    assert result == [Team]


@pytest.mark.asyncio
async def test_topological_sort_all_models(db):
    """Sorting all registered models succeeds and includes every model."""
    all_models = list(Hare.apps.get_models_iterable())
    sorted_models = topological_sort_models(all_models)
    assert set(sorted_models) == set(all_models)


@pytest.mark.asyncio
async def test_topological_sort_multi_level_chain(db):
    """MinRelation→Tournament and MinRelation→Team: MinRelation before both parents."""
    sorted_models = topological_sort_models([Tournament, Team, MinRelation])
    assert sorted_models.index(MinRelation) < sorted_models.index(Tournament)
    assert sorted_models.index(MinRelation) < sorted_models.index(Team)


@pytest.mark.asyncio
async def test_topological_sort_multiple_fks_on_one_model(db):
    """Event has FKs to both Tournament and Reporter — must come before both."""
    sorted_models = topological_sort_models([Tournament, Reporter, Event])
    assert sorted_models.index(Event) < sorted_models.index(Tournament)
    assert sorted_models.index(Event) < sorted_models.index(Reporter)


# ---------------------------------------------------------------------------
# truncate_all_models — integration tests against real DB
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_truncate_empty_db(db):
    """Truncating when tables are empty should succeed without error."""
    await truncate_all_models()


@pytest.mark.asyncio
async def test_truncate_clears_data(db):
    """Data created before truncation is gone after truncation."""
    tournament = await Tournament.objects.create(name="Test Tournament")
    await Event.objects.create(name="Test Event", tournament=tournament)

    await truncate_all_models()

    assert await Tournament.objects.all().count() == 0
    assert await Event.objects.all().count() == 0


@pytest.mark.asyncio
async def test_truncate_with_fk_constraints(db):
    """Truncation succeeds even with FK constraints (child→parent)."""
    t = await Tournament.objects.create(name="T1")
    await Event.objects.create(name="E1", tournament=t)
    await Event.objects.create(name="E2", tournament=t)

    # This would fail with arbitrary order on strict FK enforcement
    await truncate_all_models()

    assert await Event.objects.all().count() == 0
    assert await Tournament.objects.all().count() == 0


@pytest.mark.asyncio
async def test_truncate_with_self_referential_fk(db):
    """Self-referential FK (Employee→Employee) doesn't break truncation."""
    boss = await Employee.objects.create(name="Boss")
    await Employee.objects.create(name="Worker", manager=boss)

    await truncate_all_models()

    assert await Employee.objects.all().count() == 0


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_truncate_all_models_spans_multiple_connections(two_databases):  # noqa: F811
    """truncate_all_models() picked a single connection (models[0]._meta.db) to determine both
    the dialect check AND to run the actual TRUNCATE statement, even though
    Hare.apps.get_models_iterable() can span multiple connections in a multi-DB setup (the
    two_databases fixture registers Tournament/Event on the "models" connection and EventTwo on
    a separate "events" connection/database) - the single TRUNCATE statement referenced tables
    that don't exist on the connection it actually ran against, crashing outright."""
    tournament = await Tournament.objects.create(name="Tournament")
    await Event.objects.create(name="Event1", tournament=tournament)
    await EventTwo.objects.create(name="Event2", tournament_id=tournament.id)

    await truncate_all_models()

    assert await Tournament.objects.all().count() == 0
    assert await Event.objects.all().count() == 0
    assert await EventTwo.objects.all().count() == 0


@pytest.mark.asyncio
async def test_truncate_raises_when_apps_not_loaded(db_simple):
    """truncate_all_models raises ConfigurationError when apps aren't loaded."""
    from hare.core.hare_context import HareContext
    from hare.exceptions import ConfigurationError

    ctx = HareContext.get_current()
    saved_apps = ctx._apps
    ctx._apps = {}
    try:
        with pytest.raises(ConfigurationError, match="apps are not loaded"):
            await truncate_all_models()
    finally:
        ctx._apps = saved_apps


@pytest.mark.asyncio
async def test_truncate_clears_auto_created_m2m_through_table(db):
    """Auto-created M2M through tables aren't registered models - their rows used to survive
    truncation on SQLite and resurface on freshly created rows reusing the same ids."""
    tournament = await Tournament.objects.create(id=1, name="T")
    event = await Event.objects.create(event_id=1, name="E", tournament=tournament)
    team = await Team.objects.create(id=1, name="Team")
    await event.participants.add(team)

    await truncate_all_models()

    tournament = await Tournament.objects.create(id=1, name="T")
    event = await Event.objects.create(event_id=1, name="E", tournament=tournament)
    await Team.objects.create(id=1, name="Team")
    assert await event.participants.all().count() == 0
