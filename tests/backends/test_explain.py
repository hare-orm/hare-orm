import pytest

from hare.contrib.test import capture_queries, requires_features
from hare.contrib.test.conditions.not_eq import NotEQ
from hare.exceptions import QueryError, UnSupportedError
from tests.shared_models.testmodels import IntFields, Tournament
from tests.utils.explain_plan_checks import ExplainPlanChecks


@pytest.mark.asyncio
async def test_explain(db):
    """The server returns a structured plan for reading the model's table."""
    plan = await Tournament.objects.all().explain()
    ExplainPlanChecks.assert_read_plan(plan, Tournament.get_connection().dialect.name, Tournament._meta.db_table)


@pytest.mark.asyncio
async def test_explain_on_values_query(db):
    """explain() was only ever defined on QuerySet - .values()/.values_list() return
    ValuesQuery/ValuesListQuery, which subclass AwaitableQuery (not QuerySet) and had no
    explain() of their own at all, raising AttributeError rather than working."""
    for query in (
        Tournament.objects.values("id", "name"),
        Tournament.objects.values_list("id", "name"),
        Tournament.objects.values_list("id", flat=True),
    ):
        plan = await query.explain()
        ExplainPlanChecks.assert_read_plan(plan, Tournament.get_connection().dialect.name, Tournament._meta.db_table)


@pytest.mark.asyncio
async def test_explain_on_union_query(db):
    """explain() was only ever defined on QuerySet/AwaitableQuery - UnionQuery doesn't inherit
    from AwaitableQuery (its final SQL is built into self._union_query, a merged
    SetOperationQuery, not self.query) and had no explain() of its own at all, raising
    AttributeError rather than working."""
    await Tournament.objects.create(name="A")
    await Tournament.objects.create(name="B")
    plan = await Tournament.objects.filter(name="A").union(Tournament.objects.filter(name="B")).explain()
    ExplainPlanChecks.assert_read_plan(plan, Tournament.get_connection().dialect.name, Tournament._meta.db_table)


@pytest.mark.parametrize(
    ("options", "message"),
    [
        pytest.param(
            {"output_format": "json"}, "does not support different explain formats", id="unsupported_output_fmt"
        ),
        pytest.param({"analyze": True}, "does not support explain options", id="unsupported_options"),
    ],
)
@requires_features(dialect=NotEQ("postgresql"))
@pytest.mark.asyncio
async def test_explain_refuses_what_the_dialect_lacks(db, options, message):
    await Tournament.objects.create(name="Test")
    with pytest.raises(UnSupportedError, match=message):
        await Tournament.objects.all().explain(**options)


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
    obj = await IntFields.objects.create(intnum=1)
    obj.intnum = 2
    for query in (
        IntFields.objects.bulk_create([IntFields(intnum=3)]),
        IntFields.objects.bulk_update([obj], fields=["intnum"]),
    ):
        explain_sql = query.sql(explain=True)
        assert explain_sql.startswith("EXPLAIN")
        async with capture_queries() as counter:
            # A plain INSERT's plan can be empty - SQLite lists only the foreign key checks.
            assert isinstance(await query.explain(), list)
        assert ";".join(counter.queries) == explain_sql
    assert await IntFields.objects.all().order_by("id").values_list("intnum", flat=True) == [1]


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
