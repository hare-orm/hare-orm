import re
from decimal import Decimal

import pytest

from hare import Connections
from hare.dialects.postgresql.constants import POSTGRESQL_DIALECT
from hare.dialects.postgresql.query import PostgresqlQuery
from hare.dialects.sqlite.constants import SQLITE_DIALECT
from hare.query.expressions import F, Value
from hare.query.functions import Coalesce, Concat
from hare.sql import Criterion, EmptyCriterion, Field, Table, functions as sql_functions
from hare.sql.exceptions import FunctionException
from hare.sql.functions import Count
from hare.sql.sql_context import DEFAULT_SQL_CONTEXT, NEUTRAL_SQL_CONTEXT
from hare.sql.terms import CustomFunction, Interval, Not, Parameterizer
from tests.testmodels import CharPkModel, DecimalFields, Drink, Event, FloatFields, IntFields


@pytest.fixture
def sql_context(db):
    """Fixture providing database connection and dialect."""
    db_conn = Connections.get("models")
    dialect = db_conn.dialect.name
    if dialect not in ("sqlite", "postgresql"):
        pytest.skip("checks the SQL text hare writes for SQLite and PostgreSQL")
    return db_conn, dialect


def test_filter(sql_context):
    db, dialect = sql_context
    sql = CharPkModel.objects.all().filter(id="123").sql()
    if dialect == "postgresql":
        expected = 'SELECT "id" FROM "charpkmodel" WHERE "id"=$1'
    else:
        expected = 'SELECT "id" FROM "charpkmodel" WHERE "id"=?'

    assert sql == expected


def test_filter_with_limit_offset(sql_context):
    db, dialect = sql_context
    sql = CharPkModel.objects.all().filter(id="123").limit(10).offset(0).sql()
    if dialect == "postgresql":
        expected = 'SELECT "id" FROM "charpkmodel" WHERE "id"=$1 LIMIT $2 OFFSET $3'
    else:
        expected = 'SELECT "id" FROM "charpkmodel" WHERE "id"=? LIMIT ? OFFSET ?'

    assert sql == expected


def test_group_by(sql_context):
    db, dialect = sql_context
    sql = IntFields.objects.all().group_by("intnum").values("intnum").sql()
    expected = 'SELECT "intnum" "intnum" FROM "intfields" GROUP BY "intnum"'
    assert sql == expected


def test_annotate(sql_context):
    db, dialect = sql_context
    sql = CharPkModel.objects.all().annotate(id_plus_one=Concat(F("id"), "_postfix")).sql()
    if dialect == "postgresql":
        expected = 'SELECT "id",CONCAT("id"::text,$1::text) "id_plus_one" FROM "charpkmodel"'
    else:
        expected = 'SELECT "id",(COALESCE("id", \'\') || COALESCE(?, \'\')) "id_plus_one" FROM "charpkmodel"'
    assert sql == expected


def test_annotate_concat_fields(sql_context):
    db, dialect = sql_context
    sql = CharPkModel.objects.all().annotate(id_double=Concat(F("id"), F("id"))).sql()
    if dialect == "postgresql":
        expected = 'SELECT "id",CONCAT("id"::text,"id"::text) "id_double" FROM "charpkmodel"'
    else:
        expected = 'SELECT "id",(COALESCE("id", \'\') || COALESCE("id", \'\')) "id_double" FROM "charpkmodel"'
    assert sql == expected


def test_annotate_concat_non_string_literal(sql_context):
    db, dialect = sql_context
    sql = CharPkModel.objects.all().annotate(tagged=Concat(F("id"), Value(123), "_postfix")).values("tagged").sql()
    if dialect == "postgresql":
        expected = 'SELECT CONCAT("id"::text,$1::BIGINT,$2::text) "tagged" FROM "charpkmodel"'
    else:
        expected = (
            "SELECT (COALESCE(\"id\", '') || COALESCE(?, '') || COALESCE(?, '')) \"tagged\" FROM \"charpkmodel\""
        )
    assert sql == expected


def test_annotate_coalesce_field_expression(sql_context):
    db, dialect = sql_context
    sql = IntFields.objects.all().annotate(num=Coalesce("intnum", F("intnum_null"))).values("num").sql()
    expected = 'SELECT COALESCE("intnum","intnum_null") "num" FROM "intfields"'
    assert sql == expected


def test_annotate_coalesce_compatible_literal_default_is_not_cast(sql_context):
    db, dialect = sql_context
    sql = IntFields.objects.all().annotate(num=Coalesce("intnum_null", 5)).values("num").sql()
    placeholder = "$1" if dialect == "postgresql" else "?"
    assert sql == f'SELECT COALESCE("intnum_null",{placeholder}) "num" FROM "intfields"'


def test_annotate_coalesce_incompatible_numeric_literal_default_is_cast(sql_context):
    db, dialect = sql_context
    sql = IntFields.objects.all().annotate(num=Coalesce("intnum_null", Decimal("5.55"))).values("num").sql()
    placeholder = "$1" if dialect == "postgresql" else "?"
    assert sql == f'SELECT COALESCE("intnum_null",CAST({placeholder} AS NUMERIC)) "num" FROM "intfields"'


def test_annotate_coalesce_incompatible_value_default_is_cast(sql_context):
    db, dialect = sql_context
    placeholder = "$1" if dialect == "postgresql" else "?"
    incompatible = (
        IntFields.objects.all().annotate(num=Coalesce("intnum_null", Value(Decimal("5.55")))).values("num").sql()
    )
    assert incompatible == f'SELECT COALESCE("intnum_null",CAST({placeholder} AS NUMERIC)) "num" FROM "intfields"'
    compatible = IntFields.objects.all().annotate(num=Coalesce("intnum_null", Value(5))).values("num").sql()
    assert compatible == f'SELECT COALESCE("intnum_null",{placeholder}) "num" FROM "intfields"'


def test_annotate_coalesce_wider_numeric_default_cast_for_decimal_and_float_fields(sql_context):
    db, dialect = sql_context
    placeholder = "$1" if dialect == "postgresql" else "?"
    for model, field_name, table, default in (
        (DecimalFields, "decimal_null", "decimalfields", 0.0),
        (DecimalFields, "decimal_null", "decimalfields", Decimal("1.25")),
        (DecimalFields, "decimal_null", "decimalfields", Value(0.5)),
        (FloatFields, "floatnum_null", "floatfields", Decimal("2.5")),
        (FloatFields, "floatnum_null", "floatfields", Value(Decimal("2.5"))),
    ):
        sql = model.objects.all().annotate(num=Coalesce(field_name, default)).values("num").sql()
        # SQLite wraps a DecimalField column itself in CAST(... AS NUMERIC) - unrelated to the
        # default value, which is what must stay uncast here.
        column_sql = (
            f'CAST("{field_name}" AS NUMERIC)'
            if dialect != "postgresql" and model is DecimalFields
            else f'"{field_name}"'
        )
        # A Decimal default is cast to NUMERIC on SQLite only (it binds as TEXT there and would
        # otherwise compare above every number).
        default_literal = default.value if isinstance(default, Value) else default
        default_sql = (
            f"CAST({placeholder} AS NUMERIC)"
            if dialect != "postgresql" and isinstance(default_literal, Decimal)
            else placeholder
        )
        if model is DecimalFields and isinstance(default_literal, float):
            # A float default makes the result a float - cast so Postgres doesn't type it numeric.
            default_sql = f"CAST({placeholder} AS FLOAT)"
        assert sql == f'SELECT COALESCE({column_sql},{default_sql}) "num" FROM "{table}"', default


def test_lower_upper_udf_calls_stay_out_of_neutral_sql_context():
    """Lower()/Upper() render calls to hare's own SQLite UDFs for a real SQLite query, but the SQL
    text stored in an index definition or a written migration file must stay native/portable -
    an expression index can't call a non-deterministic UDF, and no other dialect has one."""
    lower_term = sql_functions.Lower("Name")
    upper_term = sql_functions.Upper("Name")

    sqlite_context = DEFAULT_SQL_CONTEXT.copy(dialect=SQLITE_DIALECT)
    assert lower_term.get_sql(sqlite_context) == "hare_lower('Name')"
    assert upper_term.get_sql(sqlite_context) == "hare_upper('Name')"
    assert lower_term.get_sql(NEUTRAL_SQL_CONTEXT) == "LOWER('Name')"
    assert upper_term.get_sql(NEUTRAL_SQL_CONTEXT) == "UPPER('Name')"
    assert lower_term.get_sql(DEFAULT_SQL_CONTEXT.copy(dialect=POSTGRESQL_DIALECT)) == "LOWER('Name')"


def test_annotate_function_join_expression(sql_context):
    db, dialect = sql_context
    qset = Event.objects.all().annotate(full_name=Concat("name", F("tournament__name"))).values("full_name")
    sql = qset.sql()
    join_match = (
        r'LEFT OUTER JOIN [`"]tournament[`"] [`"]event__tournament[`"] ON '
        r'[`"]event__tournament[`"]\.[`"]id[`"]=[`"]event[`"]\.[`"]tournament_id[`"]'
    )
    assert re.search(join_match, sql)
    if dialect == "postgresql":
        concat_match = r'CONCAT\("event"\."name"::text,"event__tournament"\."name"::text\)'
    else:
        concat_match = r'\(COALESCE\("event"\."name", \'\'\) \|\| COALESCE\("event__tournament"\."name", \'\'\)\)'
    assert re.search(concat_match, sql)


def test_values(sql_context):
    db, dialect = sql_context
    sql = IntFields.objects.filter(intnum=1).values("intnum").sql()
    if dialect == "postgresql":
        expected = 'SELECT "intnum" "intnum" FROM "intfields" WHERE "intnum"=$1'
    else:
        expected = 'SELECT "intnum" "intnum" FROM "intfields" WHERE "intnum"=?'
    assert sql == expected


def test_values_list(sql_context):
    db, dialect = sql_context
    sql = IntFields.objects.filter(intnum=1).values_list("intnum").sql()
    if dialect == "postgresql":
        expected = 'SELECT "intnum" "0" FROM "intfields" WHERE "intnum"=$1'
    else:
        expected = 'SELECT "intnum" "0" FROM "intfields" WHERE "intnum"=?'
    assert sql == expected


def test_exists(sql_context):
    db, dialect = sql_context
    sql = IntFields.objects.filter(intnum=1).exists().sql()
    if dialect == "postgresql":
        expected = 'SELECT 1 FROM "intfields" WHERE "intnum"=$1 LIMIT $2'
    else:
        expected = 'SELECT 1 FROM "intfields" WHERE "intnum"=? LIMIT ?'
    assert sql == expected


def test_count(sql_context):
    db, dialect = sql_context
    sql = IntFields.objects.all().filter(intnum=1).count().sql()
    if dialect == "postgresql":
        expected = 'SELECT COUNT(*) FROM "intfields" WHERE "intnum"=$1'
    else:
        expected = 'SELECT COUNT(*) FROM "intfields" WHERE "intnum"=?'
    assert sql == expected


def test_update(sql_context):
    db, dialect = sql_context
    sql = IntFields.objects.filter(intnum=2).update(intnum=1).sql()
    if dialect == "postgresql":
        expected = 'UPDATE "intfields" SET "intnum"=$1 WHERE "intnum"=$2'
    else:
        expected = 'UPDATE "intfields" SET "intnum"=? WHERE "intnum"=?'
    assert sql == expected


def test_delete(sql_context):
    db, dialect = sql_context
    sql = IntFields.objects.filter(intnum=2).delete().sql()
    if dialect == "postgresql":
        expected = 'DELETE FROM "intfields" WHERE "intnum"=$1'
    else:
        expected = 'DELETE FROM "intfields" WHERE "intnum"=?'
    assert sql == expected


@pytest.mark.asyncio
async def test_bulk_update(sql_context):
    db, dialect = sql_context
    obj1 = await IntFields.objects.create(intnum=1)
    obj2 = await IntFields.objects.create(intnum=2)
    obj1.intnum = obj1.intnum + 1
    obj2.intnum = obj2.intnum + 1
    sql = IntFields.objects.bulk_update([obj1, obj2], fields=["intnum"]).sql()

    if dialect == "postgresql":
        expected = (
            'UPDATE "intfields" SET "intnum" = "hare_bulk_update_values".c1 FROM '
            "(SELECT column1 AS c0, column2 AS c1 FROM "
            '(VALUES (CAST($1 AS INT), CAST($2 AS INT)), (CAST($3 AS INT), CAST($4 AS INT))) AS "hare_values_rows") '
            'AS "hare_bulk_update_values" '
            'WHERE "intfields"."id" = "hare_bulk_update_values".c0'
        )
    else:
        expected = (
            'UPDATE "intfields" SET "intnum" = "hare_bulk_update_values".c1 FROM '
            '(SELECT column1 AS c0, column2 AS c1 FROM (VALUES (?, ?), (?, ?)) AS "hare_values_rows") '
            'AS "hare_bulk_update_values" '
            'WHERE "intfields"."id" = "hare_bulk_update_values".c0'
        )
    assert sql == expected


@pytest.mark.asyncio
async def test_bulk_create_autogenerated_pk(sql_context):
    """RETURNING "id" is unconditional here regardless of bulk_create()'s own returning=
    argument (defaulting to False) - that argument controls whether the RETURNING output is
    actually READ back onto the instances, not whether the SQL text asks for it at all.
    SQLite matches Postgres here (SqliteExecutor._prepare_insert_statement mirrors
    PostgresqlExecutor's identical override) - both dialects fully support RETURNING."""
    db, dialect = sql_context
    sql = IntFields.objects.bulk_create([IntFields(intnum=1, intnum_null=2), IntFields(intnum=3, intnum_null=4)]).sql()
    if dialect == "postgresql":
        expected = 'INSERT INTO "intfields" ("intnum","intnum_null") VALUES ($1,$2) RETURNING "id"'
    else:
        expected = 'INSERT INTO "intfields" ("intnum","intnum_null") VALUES (?,?) RETURNING "id" AS "id"'
    assert sql == expected


@pytest.mark.asyncio
async def test_bulk_create_specified_pk(sql_context):
    db, dialect = sql_context
    sql = IntFields.objects.bulk_create([IntFields(id=1, intnum=1), IntFields(id=2, intnum=2)]).sql()
    if dialect == "postgresql":
        expected = 'INSERT INTO "intfields" ("id","intnum","intnum_null") VALUES ($1,$2,$3)'
    else:
        expected = 'INSERT INTO "intfields" ("id","intnum","intnum_null") VALUES (?,?,?)'
    assert sql == expected


def test_m2m_filter_two_relations_same_target_produces_aliased_joins(sql_context):
    """Filtering on two M2M relations to the same target table should produce distinct aliased JOINs."""
    db, dialect = sql_context
    sql = Drink.objects.filter(flavors__name="vanilla", toppings__name="mint").sql()

    assert '"drink_flavor"' in sql
    assert '"drink_topping"' in sql
    assert '"drink__flavors"' in sql
    assert '"drink__toppings"' in sql


def test_empty_criterion_invert_is_a_no_op():
    """EmptyCriterion (Criterion.any()/.all()'s own builder starting point) had __and__/__or__/
    __xor__ but no __invert__ at all - ~Criterion.any(()) raised TypeError. Matches the no-op
    shape __and__/__or__/__xor__ already have: negating "no condition yet" is still "no
    condition yet", the same instance."""
    empty = Criterion.any(())
    assert isinstance(empty, EmptyCriterion)
    assert ~empty is empty


def test_mul_of_div_keeps_parens():
    a, b, c = Field("a"), Field("b"), Field("c")
    expr = a * (b / c)
    assert str(expr) == '"a"*("b"/"c")'


def test_div_of_mul_still_needs_parens():
    a, b, c = Field("a"), Field("b"), Field("c")
    expr = a / (b * c)
    assert str(expr) == '"a"/("b"*"c")'


def test_mul_of_mul_does_not_need_parens():
    a, b, c = Field("a"), Field("b"), Field("c")
    expr = a * (b * c)
    assert str(expr) == '"a"*"b"*"c"'


def test_sub_of_mul_does_not_need_parens():
    a, b, c = Field("a"), Field("b"), Field("c")
    expr = a - (b * c)
    assert str(expr) == '"a"-"b"*"c"'


def test_field_eq_none_renders_is_null():
    f = Field("x")
    assert (f == None).get_sql(DEFAULT_SQL_CONTEXT) == '"x" IS NULL'  # noqa: E711


def test_field_ne_none_renders_is_not_null():
    f = Field("x")
    assert (f != None).get_sql(DEFAULT_SQL_CONTEXT) == 'NOT "x" IS NULL'  # noqa: E711


def test_field_eq_none_is_not_a_tautologically_false_equals_null():
    f = Field("x")
    sql = (f == None).get_sql(DEFAULT_SQL_CONTEXT)  # noqa: E711
    assert "=NULL" not in sql.replace(" ", "")
    assert "<>NULL" not in sql.replace(" ", "")


CATEGORY = Table("category")
ANCESTORS = Table("ancestors")


# --- Array alias under parameterization ------------------------------------------------------------


def test_array_keeps_alias_when_parameterized():
    from hare.sql.terms import Array

    parameterizer = Parameterizer()
    ctx = DEFAULT_SQL_CONTEXT.copy(parameterizer=parameterizer)

    sql = Array(1, 2, 3).as_("arr").get_sql(ctx)

    assert "arr" in sql


# --- CustomFunction without params -----------------------------------------------------------------


def test_custom_function_without_params_keeps_args():
    func = CustomFunction("MYFUNC")
    assert str(func(1, 2, 3)) == "MYFUNC(1,2,3)"


def test_custom_function_with_params_still_validates_arity():
    func = CustomFunction("MYFUNC", ["a", "b"])
    with pytest.raises(FunctionException):
        func(1)


# --- Not.__getattr__ zero-arg delegation ------------------------------------------------------------


def test_not_delegates_zero_arg_method():
    result = Not(Count(Field("id"))).distinct()
    assert isinstance(result, Not)
    assert "DISTINCT" in str(result)


def test_not_delegates_multi_arg_method():
    f = Field("x")
    result = (~f).isin([1, 2, 3])
    assert "IN" in str(result)


# --- Interval on SQLite -------------------------------------------------------------------------


def test_interval_raises_clear_error_on_sqlite():
    ctx = DEFAULT_SQL_CONTEXT.copy(dialect=SQLITE_DIALECT)
    with pytest.raises(FunctionException, match="SQLite"):
        Interval(days=1).get_sql(ctx)


def test_interval_still_works_on_postgres():
    ctx = DEFAULT_SQL_CONTEXT.copy(dialect=POSTGRESQL_DIALECT)
    assert "INTERVAL" in Interval(days=1).get_sql(ctx)


# --- recursive CTE auto-detection ----------------------------------------------------------------


def test_non_recursive_union_cte_is_not_marked_recursive():
    other = Table("other")
    base = PostgresqlQuery.from_(CATEGORY).select(CATEGORY.id)
    step = PostgresqlQuery.from_(other).select(other.id)
    cte_query = base.union_all(step)

    q = PostgresqlQuery.with_(cte_query, "not_recursive_at_all").from_(CATEGORY).select(CATEGORY.id)

    assert "RECURSIVE" not in str(q)


def test_self_referencing_union_cte_is_marked_recursive():
    base = PostgresqlQuery.from_(CATEGORY).select(CATEGORY.id, CATEGORY.parent_id)
    step = (
        PostgresqlQuery.from_(CATEGORY)
        .join(ANCESTORS)
        .on(CATEGORY.id == ANCESTORS.parent_id)
        .select(CATEGORY.id, CATEGORY.parent_id)
    )
    cte_query = base * step  # __mul__ == union_all, matches with_cte()'s own convention
    cte_query.base_query.wrap_set_operation_queries = False

    q = PostgresqlQuery.with_(cte_query, "ancestors").from_(CATEGORY).select(CATEGORY.id)

    assert "WITH RECURSIVE" in str(q)


def test_recursion_detected_in_recursive_step_not_only_base_case():
    # the self-reference lives in the SECOND branch of the union (the recursive step) - the
    # base case alone never references "ancestors".
    base = PostgresqlQuery.from_(CATEGORY).select(CATEGORY.id, CATEGORY.parent_id)
    step = (
        PostgresqlQuery.from_(CATEGORY)
        .join(ANCESTORS)
        .on(CATEGORY.id == ANCESTORS.parent_id)
        .select(CATEGORY.id, CATEGORY.parent_id)
    )
    third = PostgresqlQuery.from_(CATEGORY).select(CATEGORY.id, CATEGORY.parent_id)
    cte_query = base.union_all(step).union_all(third)
    cte_query.base_query.wrap_set_operation_queries = False

    q = PostgresqlQuery.with_(cte_query, "ancestors").from_(CATEGORY).select(CATEGORY.id)

    assert "WITH RECURSIVE" in str(q)
