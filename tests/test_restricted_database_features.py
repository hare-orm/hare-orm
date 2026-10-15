"""A database without savepoints, generated keys, row updates, row deletes or correlated subqueries
(an analytical one): what it can't run is refused before any SQL is sent, a model it can't write is
refused when bound, and a correlated EXISTS is written as a membership test with the same rows."""

import pytest
import pytest_asyncio

from hare import Hare, fields
from hare.contrib.test import requires_features
from hare.core.apps.model_connection_checks import ModelConnectionChecks
from hare.core.registries import Registries
from hare.exceptions import ConfigurationError, UnSupportedError
from hare.models import Model
from hare.query.expressions import Exists, OuterReference, Subquery
from hare.transactions.transactions import Transactions
from tests.testmodels import (
    Event,
    HardDeleteUnconstrainedChildSetNull,
    HardDeleteUnconstrainedParent,
    Reporter,
    SoftDeleteParent,
    Team,
    Tournament,
    UUIDPkModel,
)


@pytest.fixture
def restrict_features(db, monkeypatch):
    """Turns features off for the dialect of the test connection and the connection itself - the
    SQL renders from the dialect's, the writes check the connection's."""
    connection = Tournament._meta.connection

    def restrict(**overrides):
        monkeypatch.setattr(connection.dialect, "features", connection.dialect.features.replace(**overrides))
        monkeypatch.setattr(connection, "features", connection.features.replace(**overrides))
        Registries.changed()

    yield restrict
    monkeypatch.undo()
    Registries.changed()


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_a_nested_atomic_joins_the_transaction_without_savepoints(restrict_features):
    # The test runs in a transaction of its own - the atomic() below is nested in it.
    await Tournament.objects.create(name="outer")
    restrict_features(supports_savepoints=False)
    async with Transactions.atomic():
        await Tournament.objects.create(name="inner")
    assert sorted(await Tournament.objects.filter(name__in=["outer", "inner"]).values_list("name", flat=True)) == [
        "inner",
        "outer",
    ]


@pytest.mark.asyncio
async def test_an_update_is_refused_before_sql_without_row_updates(restrict_features):
    tournament = await Tournament.objects.create(name="kept")
    restrict_features(supports_row_updates=False)
    with pytest.raises(UnSupportedError, match="doesn't update stored rows"):
        await Tournament.objects.filter(pk=tournament.pk).update(name="changed")
    tournament.name = "changed"
    with pytest.raises(UnSupportedError, match="doesn't update stored rows"):
        await tournament.save()
    assert await Tournament.objects.filter(name="kept").count() == 1


@pytest.mark.asyncio
async def test_a_delete_is_refused_before_sql_without_row_deletes(restrict_features):
    tournament = await Tournament.objects.create(name="kept")
    restrict_features(supports_row_deletes=False)
    # Without foreign keys the delete runs through hare's cascade, which checks it first.
    with pytest.raises(UnSupportedError, match="doesn't delete stored rows|needs a DELETE of Tournament rows"):
        await Tournament.objects.filter(pk=tournament.pk).delete()
    assert await Tournament.objects.filter(name="kept").count() == 1


@pytest.mark.asyncio
async def test_a_cascade_needing_an_update_writes_nothing_without_row_updates(restrict_features):
    parent = await HardDeleteUnconstrainedParent.objects.create(name="parent")
    child = await HardDeleteUnconstrainedChildSetNull.objects.create(name="child", parent=parent)
    restrict_features(supports_row_updates=False)
    with pytest.raises(UnSupportedError, match="needs a UPDATE of HardDeleteUnconstrainedChildSetNull rows"):
        await parent.delete()
    assert await HardDeleteUnconstrainedParent.objects.filter(pk=parent.pk).exists()
    assert (await HardDeleteUnconstrainedChildSetNull.objects.get(pk=child.pk)).parent_id == parent.pk


@pytest.mark.asyncio
async def test_a_model_with_a_generated_key_is_refused_without_generated_keys(restrict_features):
    restrict_features(supports_generated_keys=False)
    with pytest.raises(ConfigurationError, match=r'"models.Tournament.id" is a primary key the database generates'):
        ModelConnectionChecks.check_model_writes(Tournament)
    ModelConnectionChecks.check_model_writes(UUIDPkModel)
    # Binding the models again binds Tournament too.
    with pytest.raises(ConfigurationError, match="generates no keys"):
        Hare.register_live_models(
            [type("LiveUuidRow", (Model,), {"__module__": __name__, "id": fields.UUIDField(primary_key=True)})],
            "restricted",
            connection_alias=Tournament._meta.default_connection,
        )


@pytest.mark.asyncio
async def test_a_soft_deleted_model_is_refused_without_row_updates(restrict_features):
    restrict_features(supports_row_updates=False)
    with pytest.raises(ConfigurationError, match=r'"models.SoftDeleteParent" is soft-deleted'):
        ModelConnectionChecks.check_model_writes(SoftDeleteParent)
    ModelConnectionChecks.check_model_writes(UUIDPkModel)


@pytest_asyncio.fixture
async def calendar(db):
    first = await Tournament.objects.create(name="first")
    second = await Tournament.objects.create(name="second")
    await Tournament.objects.create(name="empty")
    anna = await Reporter.objects.create(name="anna")
    await Reporter.objects.create(name="boris")
    match = await Event.objects.create(name="match", tournament=first, reporter=anna)
    await Event.objects.create(name="final", tournament=second)
    await Event.objects.create(name="replay", tournament=second, reporter=anna)
    await Event.objects.create(name="unreported", tournament=first)
    await match.participants.add(await Team.objects.create(name="blue"))
    return first


def correlated_querysets():
    return {
        "exclude across a to-many relation": Tournament.objects.exclude(events__name="match").values_list(
            "name", flat=True
        ),
        "exclude across a nullable to-many relation": Reporter.objects.exclude(events__name="final").values_list(
            "name", flat=True
        ),
        "many-to-many isnull": Event.objects.filter(participants__isnull=True).values_list("name", flat=True),
        "many-to-many not isnull": Event.objects.filter(participants__isnull=False).values_list("name", flat=True),
        "Exists with OuterReference": Tournament.objects.annotate(
            has_reported=Exists(Event.objects.filter(tournament=OuterReference("pk"), reporter__isnull=False))
        )
        .filter(has_reported=True)
        .values_list("name", flat=True),
        "not Exists with OuterReference": Tournament.objects.annotate(
            has_events=Exists(Event.objects.filter(tournament=OuterReference("pk")))
        )
        .filter(has_events=False)
        .values_list("name", flat=True),
    }


@pytest.mark.asyncio
async def test_a_correlated_exists_runs_as_a_membership_test_with_the_same_rows(calendar, restrict_features):
    expected = {name: sorted(await queryset) for name, queryset in correlated_querysets().items()}
    restrict_features(supports_correlated_subqueries=False)
    for name, queryset in correlated_querysets().items():
        sql = queryset.sql()
        assert "EXISTS" not in sql, name
        assert " IN (" in sql, name
        assert sorted(await queryset) == expected[name], name


@pytest.mark.asyncio
async def test_a_subquery_correlated_otherwise_is_refused_before_sql(calendar, restrict_features):
    restrict_features(supports_correlated_subqueries=False)
    later_events = Event.objects.filter(tournament=OuterReference("pk"), event_id__gt=OuterReference("pk"))
    with pytest.raises(UnSupportedError, match="no correlated subqueries"):
        await Tournament.objects.annotate(later=Exists(later_events)).filter(later=True)
    first_event_name = Subquery(
        Event.objects.filter(tournament=OuterReference("pk")).order_by("name").values("name")[:1]
    )
    with pytest.raises(UnSupportedError, match="no correlated subqueries"):
        await Tournament.objects.annotate(first_event=first_event_name)


@pytest.mark.asyncio
async def test_a_delete_is_written_by_the_dialects_hook(db, monkeypatch):
    clauses = Tournament._meta.connection.dialect.clauses
    written = []
    write_delete_sql = clauses.get_delete_sql

    def recording_get_delete_sql(builder, ctx):
        sql = write_delete_sql(builder, ctx)
        written.append(sql)
        return sql

    monkeypatch.setattr(clauses, "get_delete_sql", recording_get_delete_sql)
    Registries.changed()
    try:
        tournament = await Tournament.objects.create(name="gone")
        assert await Tournament.objects.filter(pk=tournament.pk).delete() == 1
    finally:
        monkeypatch.undo()
        Registries.changed()
    assert written
    assert written[-1].startswith("DELETE FROM")
