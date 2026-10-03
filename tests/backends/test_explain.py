import pytest

from hare.contrib.test import capture_queries, requires_features
from hare.contrib.test.not_eq import NotEQ
from hare.exceptions import QueryError, UnSupportedError
from tests.testmodels import Tournament


@pytest.mark.asyncio
async def test_explain(db):
    """Test that explain() returns query plan information.

    NOTE: we do not provide any guarantee on the format of the value
    returned by `.explain()`, as it heavily depends on the database.
    This test merely checks that one is able to run `.explain()`
    without errors for each backend.
    """
    plan = await Tournament.objects.all().explain()
    # This should have returned *some* information.
    assert len(str(plan)) > 20


@pytest.mark.asyncio
async def test_explain_on_values_query(db):
    """explain() was only ever defined on QuerySet - .values()/.values_list() return
    ValuesQuery/ValuesListQuery, which subclass AwaitableQuery (not QuerySet) and had no
    explain() of their own at all, raising AttributeError rather than working."""
    plan = await Tournament.objects.all().values("id", "name").explain()
    assert len(str(plan)) > 20

    plan = await Tournament.objects.all().values_list("id", "name").explain()
    assert len(str(plan)) > 20


@pytest.mark.asyncio
async def test_explain_on_union_query(db):
    """explain() was only ever defined on QuerySet/AwaitableQuery - UnionQuery doesn't inherit
    from AwaitableQuery (its final SQL is built into self._union_query, a merged
    SetOperationQuery, not self.query) and had no explain() of its own at all, raising
    AttributeError rather than working."""
    await Tournament.objects.create(name="A")
    await Tournament.objects.create(name="B")
    plan = await Tournament.objects.filter(name="A").union(Tournament.objects.filter(name="B")).explain()
    assert len(str(plan)) > 20


@requires_features(dialect=NotEQ("postgresql"))
@pytest.mark.asyncio
async def test_explain_unsupported_output_fmt(db):
    await Tournament.objects.create(name="Test")
    with pytest.raises(UnSupportedError, match="does not support different explain formats"):
        await Tournament.objects.all().explain(output_format="json")


@requires_features(dialect=NotEQ("postgresql"))
@pytest.mark.asyncio
async def test_explain_unsupported_options(db):
    await Tournament.objects.create(name="Test")
    with pytest.raises(UnSupportedError, match="does not support explain options"):
        await Tournament.objects.all().explain(analyze=True)


@pytest.mark.asyncio
async def test_explain_runs_the_statement_sql_explain_returns(db):
    await Tournament.objects.create(name="A")
    for query in (
        Tournament.objects.filter(name="A"),
        Tournament.objects.filter(name="A").values_list("id", flat=True),
        Tournament.objects.filter(name="A").union(Tournament.objects.filter(name="B")),
        Tournament.objects.raw("SELECT * FROM tournament WHERE name = %s", ["A"]),
    ):
        async with capture_queries() as counter:
            plan = await query.explain()
        assert plan
        assert counter.queries == [query.sql(explain=True)]


@pytest.mark.asyncio
async def test_bulk_writes_explain_their_statements(db):
    tournament = await Tournament.objects.create(name="A")
    tournament.name = "B"
    for query in (
        Tournament.objects.bulk_create([Tournament(name="C")]),
        Tournament.objects.bulk_update([tournament], fields=["name"]),
    ):
        explain_sql = query.sql(explain=True)
        assert explain_sql.startswith("EXPLAIN")
        async with capture_queries() as counter:
            # A plain INSERT's plan can be empty - SQLite lists only the foreign key checks.
            assert isinstance(await query.explain(), list)
        assert ";".join(counter.queries) == explain_sql
    assert await Tournament.objects.all().order_by("id").values_list("name", flat=True) == ["A"]


@pytest.mark.asyncio
async def test_sql_rejects_explain_options_without_explain(db):
    with pytest.raises(QueryError, match="explain=True"):
        Tournament.objects.all().sql(output_format="json")
    with pytest.raises(QueryError, match="explain=True"):
        Tournament.objects.all().sql(analyze=True)


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_sqlite_explain_sql(db):
    assert Tournament.objects.all().sql(explain=True) == f"EXPLAIN QUERY PLAN {Tournament.objects.all().sql()}"


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_postgresql_explain_sql(db):
    sql = Tournament.objects.all().sql()
    assert Tournament.objects.all().sql(explain=True) == f"EXPLAIN (VERBOSE, FORMAT JSON) {sql}"
    assert (
        Tournament.objects.all().sql(explain=True, output_format="text", verbose=True, analyze=True, costs=False)
        == f"EXPLAIN (ANALYZE, VERBOSE, FORMAT TEXT) {sql}"
    )
