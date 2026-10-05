"""Regression coverage for the unescaped-quote SQL injection points fixed in 9e0956c
(ValueWrapper's plain-string and dict/list-JSON branches, JSON.get_sql()'s nested string
rendering, JSONAttributeCriterion's Postgres/SQLite path-key rendering) plus SqlContext.quote_text()'s
own identifier-quoting fix (Table/Schema/Field/Index names) - a smoke pass confirming the
normal parameterized filter/values/annotate path never falls back to inlining a value as a SQL
literal in the first place.

Every case below asserts the ADVERSARIAL value survives round-trip through the rendered SQL's
own escaping convention (undoing "doubled quote char" recovers the exact original string) -
not just "no exception was raised", which a broken-out injection wouldn't raise either.
"""

import json
import sqlite3
from contextlib import closing

import pytest

from hare.contrib.test import requires_features
from hare.dialects.postgresql.constants import POSTGRESQL_DIALECT
from hare.dialects.sqlite.constants import SQLITE_DIALECT
from hare.query.expressions import F
from hare.sql.builder import AliasedQuery, Query, Schema, Table
from hare.sql.sql_context import DEFAULT_SQL_CONTEXT
from hare.sql.terms import JSON, Field, Index, JSONAttributeCriterion, ValueWrapper
from tests.testmodels import CharFields, JSONFields

ADVERSARIAL_VALUES = [
    "plain",
    "single ' quote",
    'double " quote',
    "both ' and \" quotes",
    "; DROP TABLE widget; --",
    "backslash \\ and quote '",
    "unicode quotes ‘ ’ “ ”",
    "'''triple quoted'''",
    "",
]


def _unescape(rendered_literal: str, quote_char: str) -> str:
    """Undoes the doubled-quote-char escaping convention: strips the outer quote_char
    delimiters and collapses every doubled occurrence back to one - the inverse of what
    every fixed call site does. If the escaping was wrong, this either raises (unbalanced
    quotes) or silently returns a corrupted string that won't match the original value."""
    assert rendered_literal[0] == quote_char and rendered_literal[-1] == quote_char
    return rendered_literal[1:-1].replace(quote_char * 2, quote_char)


@pytest.mark.parametrize("value", ADVERSARIAL_VALUES)
def test_value_wrapper_plain_string_escapes_quotes(value):
    rendered = ValueWrapper.get_formatted_value(value, DEFAULT_SQL_CONTEXT)
    assert _unescape(rendered, "'") == value
    # An odd number of unescaped quote chars would mean the literal wasn't properly closed -
    # the escaped form must always be quote-balanced.
    assert rendered.count("'") % 2 == 0


@pytest.mark.parametrize("value", ADVERSARIAL_VALUES)
def test_value_wrapper_dict_json_branch_escapes_quotes(value):
    rendered = ValueWrapper.get_formatted_value({"key": value}, DEFAULT_SQL_CONTEXT)
    assert rendered.count("'") % 2 == 0
    dumped = _unescape(rendered, "'")
    assert json.loads(dumped) == {"key": value}


@pytest.mark.parametrize("value", ADVERSARIAL_VALUES)
def test_value_wrapper_list_json_branch_escapes_quotes(value):
    rendered = ValueWrapper.get_formatted_value([value], DEFAULT_SQL_CONTEXT)
    assert rendered.count("'") % 2 == 0
    dumped = _unescape(rendered, "'")
    assert json.loads(dumped) == [value]


@pytest.mark.parametrize("value", ADVERSARIAL_VALUES)
def test_json_get_sql_escapes_nested_string_values(value):
    rendered = JSON({"key": value}).get_sql(DEFAULT_SQL_CONTEXT)
    assert rendered.count("'") % 2 == 0
    inner = _unescape(rendered, "'")
    assert value in inner


@pytest.mark.parametrize("value", ADVERSARIAL_VALUES)
def test_json_attribute_criterion_postgres_escapes_path_keys(value):
    criterion = JSONAttributeCriterion(Field("data"), [value])
    ctx = DEFAULT_SQL_CONTEXT.copy(dialect=POSTGRESQL_DIALECT)
    rendered = criterion.get_sql(ctx)
    assert rendered.count("'") % 2 == 0


@pytest.mark.parametrize("value", ADVERSARIAL_VALUES)
def test_json_attribute_criterion_sqlite_escapes_path_keys(value):
    criterion = JSONAttributeCriterion(Field("data"), [value])
    ctx = DEFAULT_SQL_CONTEXT.copy(dialect=SQLITE_DIALECT)
    rendered = criterion.get_sql(ctx)
    assert rendered.count("'") % 2 == 0


def test_json_attribute_criterion_sqlite_quotes_each_segment_individually():
    """A single-segment path whose own key contains a literal dot (e.g. "user.name") must
    render to DIFFERENT SQL text than the two-segment path ["user", "name"] - a bare, unquoted
    ".part" join (the previous behavior) produced the identical "$.user.name" text for both,
    since nothing distinguished a dot that's part of a key's own name from the path separator."""
    ctx = DEFAULT_SQL_CONTEXT.copy(dialect=SQLITE_DIALECT)
    one_segment_with_dot = JSONAttributeCriterion(Field("data"), ["user.name"]).get_sql(ctx)
    two_segments = JSONAttributeCriterion(Field("data"), ["user", "name"]).get_sql(ctx)

    assert one_segment_with_dot != two_segments
    assert one_segment_with_dot == 'json_extract("data", \'$."user.name"\')'
    assert two_segments == 'json_extract("data", \'$."user"."name"\')'


def test_json_attribute_criterion_sqlite_resolves_dot_collision_semantically():
    """Executes the generated SQL against a real sqlite3 connection (not just comparing string
    shape) to prove the fix's actual effect: a single-segment path with a literal dot in the key
    reads that top-level key's own value, not the value nested two levels down under a
    coincidentally same-spelled ["user", "name"] path."""
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute("CREATE TABLE data_table (data TEXT)")
        conn.execute(
            "INSERT INTO data_table (data) VALUES (?)",
            (json.dumps({"user.name": "DOT-KEY-VALUE", "user": {"name": "NESTED-VALUE"}}),),
        )

        ctx = DEFAULT_SQL_CONTEXT.copy(dialect=SQLITE_DIALECT)
        one_segment_sql = JSONAttributeCriterion(Field("data"), ["user.name"]).get_sql(ctx)
        two_segment_sql = JSONAttributeCriterion(Field("data"), ["user", "name"]).get_sql(ctx)

        assert conn.execute(f"SELECT {one_segment_sql} FROM data_table").fetchone()[0] == "DOT-KEY-VALUE"
        assert conn.execute(f"SELECT {two_segment_sql} FROM data_table").fetchone()[0] == "NESTED-VALUE"


@pytest.mark.parametrize("value", ADVERSARIAL_VALUES)
def test_with_sql_escapes_cte_alias(value):
    """QueryBuilder._with_sql() spliced a CTE's own name directly into "WITH {name} AS (...)"
    with no quoting at all - the only identifier sink in the whole builder that wasn't run
    through SqlContext.quote_text() (Table/Schema/Field/Index/aliases all are). A malicious name like
    'x AS (SELECT 1) SELECT ... FROM other_table --' broke out of the WITH clause entirely and
    substituted a completely different query."""
    inner = Query.from_(Table("t")).select("id")
    outer = Query.from_(Table("t")).with_(inner, value).select("id")
    rendered = outer.get_sql(DEFAULT_SQL_CONTEXT)
    assert rendered.count('"') % 2 == 0
    assert _unescape(rendered.split(" AS (", 1)[0].removeprefix("WITH "), '"') == value


@pytest.mark.parametrize("value", ADVERSARIAL_VALUES)
def test_aliased_query_get_sql_escapes_cte_reference_name(value):
    """AliasedQuery.get_sql() (queries_tables.py) spliced the bare CTE-reference name straight
    into the rendered SQL with no quoting at all - the reference a query.join(AliasedQuery(name))
    renders when pointing at a CTE by name, as opposed to _with_sql()'s own WITH-header quoting
    of the same name (already covered by test_with_sql_escapes_cte_alias above) - two separate
    unescaped sinks for the same identifier."""
    name = f'cte{value}"; DROP TABLE users; --'
    rendered = AliasedQuery(name).get_sql(DEFAULT_SQL_CONTEXT)
    assert rendered.count('"') % 2 == 0
    assert _unescape(rendered, '"') == name


@pytest.mark.parametrize("value", ADVERSARIAL_VALUES)
def test_table_get_sql_escapes_quote_char_in_name(value):
    name = f'widget{value}"; DROP TABLE users; --'
    rendered = Table(name).get_sql(DEFAULT_SQL_CONTEXT)
    assert rendered.count('"') % 2 == 0
    assert _unescape(rendered, '"') == name


@pytest.mark.parametrize("value", ADVERSARIAL_VALUES)
def test_schema_get_sql_escapes_quote_char_in_name(value):
    name = f'evil_schema{value}"'
    rendered = Schema(name).get_sql(DEFAULT_SQL_CONTEXT)
    assert rendered.count('"') % 2 == 0
    assert _unescape(rendered, '"') == name


@pytest.mark.parametrize("value", ADVERSARIAL_VALUES)
def test_field_get_sql_escapes_quote_char_in_name(value):
    name = f'evil_column{value}"'
    rendered = Field(name).get_sql(DEFAULT_SQL_CONTEXT)
    assert rendered.count('"') % 2 == 0
    assert _unescape(rendered, '"') == name


@pytest.mark.parametrize("value", ADVERSARIAL_VALUES)
def test_index_get_sql_escapes_quote_char_in_name(value):
    name = f'evil_index{value}"'
    rendered = Index(name).get_sql(DEFAULT_SQL_CONTEXT)
    assert rendered.count('"') % 2 == 0
    assert _unescape(rendered, '"') == name


@pytest.mark.parametrize("value", ADVERSARIAL_VALUES)
@pytest.mark.asyncio
async def test_filter_round_trips_adversarial_value_as_bind_param(db, value):
    """The normal .filter()/.get() path must never reach the raw-literal rendering tested
    above at all - it binds every value as a query parameter. Round-tripping an adversarial
    value through create()/get()/filter() end to end is the strongest proof: if it were ever
    inlined unescaped, this would either raise a SQL syntax error or fail to find the row."""
    await CharFields.objects.create(char="x", char_null=value)
    fetched = await CharFields.objects.get(char_null=value)
    assert fetched.char_null == value


@pytest.mark.parametrize("value", ADVERSARIAL_VALUES)
@pytest.mark.asyncio
async def test_values_round_trips_adversarial_value(db, value):
    await CharFields.objects.create(char="x", char_null=value)
    rows = await CharFields.objects.filter(char="x").values("char_null")
    assert rows == [{"char_null": value}]


@pytest.mark.parametrize("value", ADVERSARIAL_VALUES)
@pytest.mark.asyncio
async def test_annotate_round_trips_adversarial_value(db, value):
    await CharFields.objects.create(char="x", char_null=value)
    rows = await CharFields.objects.filter(char="x").annotate(char_copy=Field("char_null")).values("char_copy")
    assert rows == [{"char_copy": value}]


@requires_features(dialect="postgresql")
@pytest.mark.parametrize("value", [v for v in ADVERSARIAL_VALUES if v])
@pytest.mark.asyncio
async def test_json_field_filter_round_trips_adversarial_key_and_value(db, value):
    """A JSON path filter's KEY segment is exactly what JSONAttributeCriterion escapes -
    unlike the value tests above, this exercises a caller-controlled key, not just a value."""
    await JSONFields.objects.create(data={value: "marker"})
    fetched = await JSONFields.objects.annotate(looked_up=F(f"data__{value}")).filter(looked_up="marker").first()
    assert fetched is not None
    assert fetched.data == {value: "marker"}
