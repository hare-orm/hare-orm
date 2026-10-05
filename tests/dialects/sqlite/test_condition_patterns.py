"""A text pattern in a condition of an index or a constraint on SQLite: SQLite's LIKE under
case_sensitive_like isn't deterministic, so the condition is written as GLOB - matching the rows the
lookup matches in a query, case-sensitively, wildcards of either syntax in the text matched as text."""

import pytest

from hare import fields
from hare.ddl.constraints.check_constraint import CheckConstraint
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.dialects.sqlite.clauses.sqlite_query_clauses import SqliteQueryClauses
from hare.dialects.sqlite.drivers.aiosqlite.client import AiosqliteClient, AiosqliteClientWithRegexpSupport
from hare.exceptions import IntegrityError, UnSupportedError
from hare.migrations.operations import CreateModel
from hare.migrations.state.state import State
from hare.migrations.state.state_apps import StateApps
from hare.query.expressions import Q


def test_a_like_pattern_becomes_the_glob_pattern_matching_the_same_texts():
    assert SqliteQueryClauses.get_glob_pattern("a\\%b\\_c%") == "a%b_c*"
    assert SqliteQueryClauses.get_glob_pattern("%x*y?_[z]%") == "*x[*]y[?]?[[]z]*"
    assert SqliteQueryClauses.get_glob_pattern("\\\\%") == "\\*"


@pytest.mark.asyncio
async def test_a_unique_constraint_on_a_prefix_holds_for_the_matching_rows_only():
    client = AiosqliteClient(file_path=":memory:", connection_alias="condition_patterns")
    await client.create_connection(with_db=True)
    try:
        editor = client.dialect.schema_editor_class(client, atomic=True, collect_sql=False)
        await CreateModel(
            name="Tagged",
            fields=[("id", fields.IntField(primary_key=True)), ("tag", fields.CharField(max_length=10))],
            options={
                "table": "tagged",
                "constraints": (
                    UniqueConstraint(fields=("tag",), name="tagged_star", condition=Q(tag__startswith="x*%")),
                ),
            },
        ).run("models", State(models={}, apps=StateApps()), dry_run=False, state_editor=editor)
        # Neither matches the literal prefix x*% - a wildcard of the pattern, or another case.
        for index, tag in enumerate(("xa%1", "xa%1", "X*%1", "X*%1", "x*a", "x*a"), 1):
            await client.execute_script(f"INSERT INTO tagged (id, tag) VALUES ({index}, '{tag}')")
        await client.execute_script("INSERT INTO tagged (id, tag) VALUES (10, 'x*%1')")
        with pytest.raises(IntegrityError):
            await client.execute_script("INSERT INTO tagged (id, tag) VALUES (11, 'x*%1')")
    finally:
        await client.close()


def test_a_regular_expression_operator_is_a_function_of_hare_connections_only():
    renderers = AiosqliteClient(file_path=":memory:", connection_alias="condition_patterns").dialect.renderers
    assert renderers.get_connection_only_function("""("title" REGEXP '^a')""") == "regexp"
    assert renderers.get_connection_only_function("""("title" MATCH '^a')""") == "match"
    assert renderers.get_connection_only_function("""hare_cast("ratio", 'INTEGER')""") == "hare_cast"
    assert renderers.get_connection_only_function("""("hare_title" = 'hare_x REGEXP ''MATCH''')""") is None


@pytest.mark.asyncio
async def test_a_check_constraint_with_a_regular_expression_is_refused():
    client = AiosqliteClientWithRegexpSupport(file_path=":memory:", connection_alias="condition_patterns")
    await client.create_connection(with_db=True)
    try:
        editor = client.dialect.schema_editor_class(client, atomic=True, collect_sql=False)
        operation = CreateModel(
            name="Titled",
            fields=[("id", fields.IntField(primary_key=True)), ("title", fields.CharField(max_length=10))],
            options={
                "table": "titled",
                "constraints": (CheckConstraint(check=Q(title__posix_regex="^a"), name="titled_a"),),
            },
        )
        with pytest.raises(UnSupportedError, match="needs regexp"):
            await operation.run("models", State(models={}, apps=StateApps()), dry_run=False, state_editor=editor)
    finally:
        await client.close()
