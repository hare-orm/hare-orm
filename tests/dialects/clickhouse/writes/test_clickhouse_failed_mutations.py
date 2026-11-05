"""A DELETE whose condition reads a set operation runs as an ``ALTER TABLE ... DELETE`` mutation - a
lightweight DELETE fails on one - and a mutation that fails is stopped, so the next write of the table
runs and the failed one never changes its rows later."""

import pytest

from hare.dialects.clickhouse.clauses.clickhouse_query_clauses import ClickhouseQueryClauses
from hare.dialects.clickhouse.enums import ClickhouseClause
from hare.exceptions import OperationalError
from hare.sql import Query, Table
from hare.sql.builder.tables.cte import Cte
from hare.sql.terms.subqueries.exists_term import ExistsTerm
from hare.sql.terms.term import Term
from tests.dialects.clickhouse.models import Account, Revision


@pytest.mark.database_independent
@pytest.mark.parametrize("set_operation", (False, True))
def test_mutation_checks_read_the_same_query_parts(set_operation):
    outer, source, branch, joined = (
        Table(name) for name in ("outer_rows", "source_rows", "branch_rows", "joined_rows")
    )
    nested = Query.from_(source).select(source.id)
    if set_operation:
        nested = nested.union_all(Query.from_(branch).select(branch.id))
    joined_query = nested.as_("nested_rows")
    base = Query.from_(outer).select(outer.id)
    queries = {
        "select": base.select(nested),
        "where": base.where(outer.id.isin(nested)),
        "group_by": base.groupby(nested),
        "having": base.groupby(outer.id).having(outer.id.isin(nested)),
        "order_by": base.orderby(nested),
        "join_on": base.join(joined).on(joined.id.isin(nested)),
        "join_source": base.join(joined_query).on(outer.id == joined_query.id),
        "from": base.from_(nested.as_("nested_rows")),
        "cte": base.with_(nested, "nested_rows").from_(Cte("nested_rows")),
        "update": Query.update(outer).set(outer.score, nested),
        "prewhere": base.set_dialect_clause(ClickhouseClause.PREWHERE, outer.id.isin(nested)),
        "limit_by": base.set_dialect_clause(ClickhouseClause.LIMIT_BY, (1, 0, (nested,))),
    }
    expected = {"source_rows", "branch_rows"} if set_operation else {"source_rows"}
    for clause, query in queries.items():
        names = {table.get_name_without_alias() for table in ClickhouseQueryClauses.get_read_tables(query)}
        assert names == expected | (set() if clause == "update" else {"outer_rows"}) | (
            {"joined_rows"} if clause == "join_on" else set()
        ), clause
        assert ClickhouseQueryClauses.reads_set_operation(query) is set_operation, clause


@pytest.mark.database_independent
def test_mutation_checks_keep_nested_query_protocol_and_correlation_boundaries():
    source, branch = Table("source_rows"), Table("branch_rows")
    nested = Query.from_(source).select(source.id).union_all(Query.from_(branch).select(branch.id))

    class NestedQuery(Term):
        holds_nested_query = True

        def get_nested_query(self):
            return nested

    for term in (NestedQuery(), ExistsTerm(nested)):
        names = {table.get_name_without_alias() for table in ClickhouseQueryClauses.get_read_tables(term)}
        assert names == {"source_rows", "branch_rows"}
        assert ClickhouseQueryClauses.reads_set_operation(term)

    # A field belonging to an enclosing derived query refers to its result, not another child scope.
    outer = nested.as_("outer_rows")
    correlated = outer.id == 1
    assert ClickhouseQueryClauses.get_read_tables(correlated) == []
    assert not ClickhouseQueryClauses.reads_set_operation(correlated)


async def create_revisions():
    await Revision.objects.bulk_create(
        [Revision(document_id=1, number=number, title=f"r{number}", score=float(number)) for number in (1, 2, 3)]
    )


@pytest.mark.asyncio
async def test_a_delete_by_keys_of_a_union(clickhouse_db):
    await create_revisions()

    keys = Revision.objects.filter(number=1).values("pk").union(Revision.objects.filter(number=2).values("pk"))
    await Revision.objects.filter(pk__in=keys).delete()
    assert await Revision.objects.values_list("number", flat=True) == [3]


@pytest.mark.asyncio
async def test_a_failed_mutation_leaves_the_table_writable(clickhouse_db):
    await Account.objects.create(id=1, owner="a", balance=1)
    connection = Account._meta.connection
    recovery = connection.get_write_recovery(Account._meta.basetable)

    with pytest.raises(OperationalError):
        await connection.execute_script(
            "ALTER TABLE account UPDATE balance = throwIf(1, 'failed') WHERE 1", recovery=recovery
        )
    with pytest.raises(OperationalError):
        await connection.execute(
            "ALTER TABLE account UPDATE balance = throwIf(1, 'failed') WHERE 1", recovery=recovery
        )
    rows = await connection.execute_dicts(
        "SELECT count() AS running FROM system.mutations WHERE database = currentDatabase() AND NOT is_done"
    )
    assert rows[0]["running"] == 0
    await Account.objects.filter(id=1).update(balance=5)
    assert await Account.objects.values_list("balance", flat=True) == [5]


@pytest.mark.asyncio
async def test_a_failed_statement_names_the_error_without_the_servers_stack_trace(clickhouse_db):
    """The native protocol's error text carried the server's multi-line stack trace."""
    with pytest.raises(OperationalError) as error:
        await Account._meta.connection.execute("SELECT throwIf(1, 'failed on purpose')")
    assert "failed on purpose" in str(error.value)
    assert "Stack trace" not in str(error.value)
