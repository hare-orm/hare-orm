from __future__ import annotations

from typing import TypedDict, Union, assert_type, cast

import pytest
import pytest_asyncio
from pydantic import BaseModel, TypeAdapter, ValidationError

from hare import fields
from hare.contrib import test
from hare.contrib.test.helpers import hare_test_context
from hare.core.connections import Connections
from hare.core.context import HareContext
from hare.dialects.base.results import StatementResult
from hare.exceptions import QueryError
from hare.models import Model
from hare.query.functions import Reverse
from hare.query.sql_execution import SqlQueryResult, execute_sql
from hare.sql import Query, Table, functions
from hare.sql.context import SqlContext
from hare.sql.queries import QueryBuilder
from hare.sql.terms import Parameterizer
from tests.testmodels import Tournament


class QueryModel(Model):
    id = fields.IntField(primary_key=True)
    name = fields.TextField()


class QueryRow(BaseModel):
    id: int
    name: str


class QueryRowDict(TypedDict):
    id: int
    name: str


# =============================================================================
# Tests for TestQueryApi (formerly SimpleTestCase)
# Uses custom in-memory SQLite initialization
# =============================================================================


@pytest_asyncio.fixture
async def query_api_db():
    """Fixture for QueryApi tests that initializes an in-memory SQLite database."""
    async with hare_test_context(modules=[__name__]) as ctx:
        await QueryModel.objects.create(id=1, name="alpha")
        await QueryModel.objects.create(id=2, name="beta")
        yield ctx


@pytest.mark.asyncio
async def test_execute_hare_sql(query_api_db) -> None:
    table = QueryModel.get_table()
    query = Query.from_(table).select(table.id, table.name).where(table.name == "alpha")

    result: SqlQueryResult[dict] = await execute_sql(query)

    assert result.rows == [{"id": 1, "name": "alpha"}]
    assert result.rows_affected == 1


@pytest.mark.asyncio
async def test_execute_hare_sql_metadata(query_api_db) -> None:
    table = QueryModel.get_table()
    query = Query.from_(table).select(table.id, table.name)

    result: SqlQueryResult[dict] = await execute_sql(query)

    assert result.rows_affected == 2


@pytest.mark.asyncio
async def test_execute_hare_sql_update_rows_affected(query_api_db) -> None:
    table = QueryModel.get_table()
    query = Query.update(table).set(table.name, "gamma").where(table.id == 1)

    result: SqlQueryResult[dict] = await execute_sql(query)

    assert result.rows == []
    assert result.rows_affected == 1


@pytest.mark.asyncio
async def test_execute_hare_sql_insert_rows_affected(query_api_db) -> None:
    table = QueryModel.get_table()
    query = Query.into(table).columns(table.name).insert("delta")

    result: SqlQueryResult[dict] = await execute_sql(query)

    assert result.rows == []
    assert result.rows_affected == 1


@pytest.mark.asyncio
async def test_query_parameterization(query_api_db) -> None:
    table = QueryModel.get_table()
    query = Query.from_(table).select(table.id).where(table.name == "alpha")
    db = Connections.get("default")

    sql, params = query.get_parameterized_sql(db.query_class.SQL_CONTEXT)

    assert "alpha" in params
    assert "alpha" not in sql


@pytest.mark.asyncio
async def test_execute_hare_sql_pydantic_schema(query_api_db) -> None:
    table = QueryModel.get_table()
    query = Query.from_(table).select(table.id, table.name).where(table.name == "alpha")

    result = cast(SqlQueryResult[QueryRow], await execute_sql(query, schema=QueryRow))

    assert isinstance(result.rows[0], QueryRow)
    assert result.rows[0].model_dump() == {"id": 1, "name": "alpha"}


@pytest.mark.asyncio
async def test_execute_hare_sql_pydantic_type_adapter(query_api_db) -> None:
    table = QueryModel.get_table()
    query = Query.from_(table).select(table.id, table.name).where(table.name == "alpha")
    adapter = TypeAdapter(dict[str, int | str])

    result: SqlQueryResult[dict[str, Union[int, str]]] = await execute_sql(  # noqa: UP007
        query,
        schema=adapter,
    )

    assert result.rows == [{"id": 1, "name": "alpha"}]


@pytest.mark.asyncio
async def test_execute_hare_sql_typed_dict_schema(query_api_db) -> None:
    table = QueryModel.get_table()
    query = Query.from_(table).select(table.id, table.name).where(table.name == "alpha")

    result: SqlQueryResult[QueryRowDict] = await execute_sql(query, schema=QueryRowDict)

    assert_type(result, SqlQueryResult[QueryRowDict])
    assert_type(result.rows, list[QueryRowDict])
    assert result.rows == [{"id": 1, "name": "alpha"}]


@pytest.mark.asyncio
async def test_execute_hare_sql_empty_result(query_api_db) -> None:
    table = QueryModel.get_table()
    query = Query.from_(table).select(table.id, table.name).where(table.name == "missing")

    result: SqlQueryResult[dict] = await execute_sql(query)

    assert result.rows == []
    assert result.rows_affected == 0


@pytest.mark.asyncio
async def test_execute_hare_sql_invalid_schema_raises(query_api_db) -> None:
    table = QueryModel.get_table()
    query = Query.from_(table).select(table.name.as_("id"), table.name)

    with pytest.raises(ValidationError):
        await execute_sql(query, schema=QueryRow)


# =============================================================================
# Tests for TestQueryApiRowsAffected (formerly test.TestCase)
# Uses db fixture with Tournament model
# =============================================================================


def _is_asyncpg(db) -> bool:
    return "hare.dialects.postgresql.drivers.asyncpg" in type(db.db()).__module__


def _is_psycopg(db) -> bool:
    return "hare.dialects.psycopg" in type(db.db()).__module__


def _is_odbc(db) -> bool:
    return "hare.dialects.odbc" in type(db.db()).__module__


def _select_query() -> QueryBuilder:
    table = Tournament.get_table()
    return Query.from_(table).select(table.id, table.name).orderby(table.id)


def _sql_context(db) -> SqlContext:
    ctx = db.db().query_class.SQL_CONTEXT
    if _is_psycopg(db) and ctx.parameterizer is None:
        ctx = ctx.copy(parameterizer=Parameterizer(placeholder_factory=lambda _: "%s"))
    return ctx


@pytest_asyncio.fixture
async def rows_affected_setup(db):
    """Fixture to set up Tournament data for rows_affected tests."""
    alpha = await Tournament.objects.create(name="alpha")
    beta = await Tournament.objects.create(name="beta")
    return {"alpha_id": alpha.id, "beta_id": beta.id, "db": db}


@test.requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_rows_affected_select_sqlite(rows_affected_setup) -> None:
    result: SqlQueryResult[dict] = await execute_sql(_select_query())

    assert result.rows_affected == len(result.rows)
    assert len(result.rows) == 2


@test.requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_rows_affected_select_asyncpg(rows_affected_setup) -> None:
    db = rows_affected_setup["db"]
    if not _is_asyncpg(db):
        pytest.skip("asyncpg only")

    result: SqlQueryResult[dict] = await execute_sql(_select_query())

    assert result.rows_affected == len(result.rows)
    assert len(result.rows) == 2


@pytest.mark.asyncio
async def test_rows_affected_select_driver_rowcount(rows_affected_setup) -> None:
    db = rows_affected_setup["db"]
    if not (_is_odbc(db) or _is_psycopg(db)):
        pytest.skip("odbc/psycopg only")

    query: QueryBuilder = _select_query()
    sql, params = query.get_parameterized_sql(_sql_context(db))
    raw_rowcount, _ = await db.db().execute(sql, params)
    result: SqlQueryResult[dict] = await execute_sql(query)

    expected = {raw_rowcount, len(result.rows)}
    assert result.rows_affected in expected


@pytest.mark.asyncio
async def test_rows_affected_update(rows_affected_setup) -> None:
    alpha_id = rows_affected_setup["alpha_id"]
    table = Tournament.get_table()
    query = Query.update(table).set(table.name, "gamma").where(table.id == alpha_id)

    result: SqlQueryResult[dict] = await execute_sql(query)

    assert result.rows == []
    assert result.rows_affected == 1


@pytest.mark.asyncio
async def test_rows_affected_delete(rows_affected_setup) -> None:
    beta_id = rows_affected_setup["beta_id"]
    table = Tournament.get_table()
    query = Query.from_(table).delete().where(table.id == beta_id)

    result: SqlQueryResult[dict] = await execute_sql(query)

    assert result.rows == []
    assert result.rows_affected == 1


# =============================================================================
# Tests for TestQueryApiConnectionSelection (formerly SimpleTestCase)
# Tests connection selection behavior
# =============================================================================


@pytest.mark.asyncio
async def test_execute_hare_sql_explicit_connection_with_multiple_configured() -> None:
    """Test execute_sql with explicit connection when multiple are configured."""

    class DummyClient:
        query_class = type("QueryClass", (), {"SQL_CONTEXT": None})
        _bound_loop = None

        def _check_loop(self) -> bool:
            return True

        async def execute(self, query, values=None, *, returns_rows=None):
            return StatementResult(0, [])

        @staticmethod
        def row_to_dict(row):
            return dict(row)

    async with HareContext() as ctx:
        await ctx.init(
            config={
                "connections": {
                    "first": "sqlite://:memory:",
                    "second": "sqlite://:memory:",
                },
                "apps": {
                    "models": {"models": [__name__], "default_connection": "first"},
                },
            }
        )
        await ctx.generate_schemas()

        query = Query.from_(Table("dummy")).select("*")

        token = ctx.connections.set("second", DummyClient())  # type: ignore[arg-type]
        try:
            result: SqlQueryResult[dict] = await execute_sql(query, using=ctx.connections.get("second"))
        finally:
            ctx.connections.reset(token)

        assert result.rows_affected == 0


@pytest_asyncio.fixture
async def multi_db():
    """Fixture that sets up multiple databases for testing."""
    from hare.core.context import HareContext

    ctx = HareContext()
    async with ctx:
        await ctx.init(
            config={
                "connections": {
                    "first": "sqlite://:memory:",
                    "second": "sqlite://:memory:",
                },
                "apps": {
                    "models": {"models": [__name__], "default_connection": "first"},
                },
            }
        )
        await ctx.generate_schemas()
        yield ctx


@pytest.mark.asyncio
async def test_execute_hare_sql_requires_connection_with_multiple_configured(multi_db) -> None:
    query = Query.from_(Table("dummy")).select("*")

    with pytest.raises(QueryError) as exc_info:
        await execute_sql(query)

    assert "multiple databases" in str(exc_info.value)


@pytest.mark.asyncio
async def test_builder_functions_run_on_every_dialect(db) -> None:
    await Tournament.objects.create(id=1, name="abc")
    await Tournament.objects.create(id=2, name="xyz")
    table = Tournament.get_table()
    connection = Tournament._meta.db

    row_query = Query.from_(table).select(functions.Now().as_("now")).where(table.id == 1)
    rows = (await execute_sql(row_query, using=connection)).rows
    assert rows[0]["now"] is not None
    # A function the dialect renders its own way (a UDF on SQLite) is the query layer's.
    assert await Tournament.objects.filter(id=1).annotate(reversed=Reverse("name")).values_list(
        "reversed", flat=True
    ) == ["cba"]

    aggregate_query = Query.from_(table).select(functions.StdDev(table.id).as_("deviation"))
    deviation = (await execute_sql(aggregate_query, using=connection)).rows[0]["deviation"]
    assert abs(float(deviation) - 0.7071067811865476) < 1e-9
