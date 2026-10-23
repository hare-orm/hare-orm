"""A statement plan is kept for the features of the connection it was built on - another connection of
the same name and dialect with other features (a context opened again, an extension loaded) builds its
own, as a plan of the other features would skip a refusal or write SQL the database doesn't take."""

import pytest

from hare.dialects.base.features import Features
from hare.query.plans.statement.statement_plans import StatementPlans
from tests.shared_models.testmodels import Tournament


@pytest.mark.database_independent
def test_equal_features_are_one_set_of_features():
    features = Features(supports_transactions=True)

    assert features.replace(supports_savepoints=True) is features.replace(supports_savepoints=True)
    assert features.replace(supports_savepoints=True).plan_key != features.replace(supports_savepoints=False).plan_key
    assert Features().plan_key != Features().plan_key


@pytest.mark.asyncio
async def test_a_connection_of_other_features_keeps_its_own_plans(db, monkeypatch):
    """The plan of a query built on a connection without SpatiaLite ran on one with it under the same
    name - a spatial lookup's refusal skipped."""
    await Tournament.objects.create(name="first")
    await Tournament.objects.filter(name="first").count()
    hits = StatementPlans.hits
    assert await Tournament.objects.filter(name="second").count() == 0
    assert StatementPlans.hits == hits + 1

    connection = Tournament._meta.connection
    other_features = connection.features.replace(max_bind_parameters=connection.features.max_bind_parameters + 1)
    monkeypatch.setattr(connection, "features", other_features)
    assert await Tournament.objects.filter(name="first").count() == 1
    assert StatementPlans.hits == hits + 1
    assert await Tournament.objects.filter(name="third").count() == 0
    assert StatementPlans.hits == hits + 2
