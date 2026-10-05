"""A model union(), a save() SETting a field from an expression, a bulk_create() upsert's tenant
scope and enum values bind every literal as a parameter - none reaches the SQL text (logs, error
messages, statement attributes). JSON path keys stay literals an expression index matches, read
the same by a Postgres session with standard_conforming_strings=off."""

from contextlib import asynccontextmanager
from enum import IntEnum, StrEnum

import pytest

from hare.contrib import test
from hare.contrib.test import capture_queries, requires_features
from hare.core.connections.connections import Connections
from hare.dialects.postgresql.constants import POSTGRESQL_DIALECT
from hare.dialects.sqlite.constants import SQLITE_DIALECT
from hare.exceptions import OperationalError, ValidationError
from hare.models.tenancy.tenancy import Tenancy
from hare.query.expressions import F, RawSQL, Value
from hare.query.functions import Coalesce, Concat, Count, Max
from hare.sql.sql_context import DEFAULT_SQL_CONTEXT
from hare.sql.terms import Field, JSONAttributeCriterion
from hare.transactions.transactions import Transactions
from tests.testmodels import (
    CharFields,
    IntFields,
    JSONFields,
    SlugTenantAccount,
    TenantActiveAuthor,
    Tournament,
    UpsertTarget,
)

SECRET_TOKEN = "RESET-TOKEN-7f3a9c"
QUOTE_BREAKING_TEXT = "x\\' OR 1=1 --"


def _assert_not_in_sql(queries: list[str], literal: str) -> None:
    assert queries
    assert not any(literal in query for query in queries), queries


@pytest.mark.asyncio
async def test_model_union_binds_filter_values(db):
    first = await Tournament.objects.create(id=1, name=SECRET_TOKEN)
    second = await Tournament.objects.create(id=2, name=QUOTE_BREAKING_TEXT)
    await Tournament.objects.create(id=3, name="other")
    union = Tournament.objects.filter(name=SECRET_TOKEN).union(Tournament.objects.filter(name=QUOTE_BREAKING_TEXT))
    async with capture_queries() as counter:
        rows = await union.order_by("id").limit(5).offset(0)
    assert [row.pk for row in rows] == [first.pk, second.pk]
    _assert_not_in_sql(counter.queries, SECRET_TOKEN)
    _assert_not_in_sql(counter.queries, QUOTE_BREAKING_TEXT)


@pytest.mark.asyncio
async def test_nested_set_operation_binds_filter_values(db):
    await Tournament.objects.create(id=1, name=SECRET_TOKEN)
    await Tournament.objects.create(id=2, name="other")
    union = (
        Tournament.objects.filter(name=SECRET_TOKEN)
        .union(Tournament.objects.filter(name="other"))
        .intersection(Tournament.objects.filter(name__in=[SECRET_TOKEN, "missing"]))
    )
    async with capture_queries() as counter:
        rows = await union
    assert [row.name for row in rows] == [SECRET_TOKEN]
    _assert_not_in_sql(counter.queries, SECRET_TOKEN)


@pytest.mark.asyncio
async def test_model_union_explain_binds_filter_values(db):
    await Tournament.objects.create(id=1, name=SECRET_TOKEN)
    async with capture_queries() as counter:
        plan = (
            await Tournament.objects.filter(name=SECRET_TOKEN).union(Tournament.objects.filter(name="other")).explain()
        )
    assert plan
    _assert_not_in_sql(counter.queries, SECRET_TOKEN)


@test.requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_model_union_error_message_carries_no_filter_value(db):
    await Tournament.objects.create(id=1, name=SECRET_TOKEN)
    zero_division = F("id") / (F("id") - F("id"))
    union = (
        Tournament.objects.filter(name=SECRET_TOKEN)
        .annotate(ratio=zero_division)
        .union(Tournament.objects.filter(name="other").annotate(ratio=zero_division))
    )
    with pytest.raises(OperationalError) as error_info:
        await union
    assert SECRET_TOKEN not in str(error_info.value)


@pytest.mark.asyncio
async def test_save_binds_an_expression_literal(db):
    tournament = await Tournament.objects.create(id=1, name="alpha")
    tournament.name = Concat(F("name"), Value(SECRET_TOKEN))
    async with capture_queries() as counter:
        await tournament.save()
    _assert_not_in_sql(counter.queries, SECRET_TOKEN)
    assert (await Tournament.objects.get(id=1)).name == f"alpha{SECRET_TOKEN}"

    tournament = await Tournament.objects.get(id=1)
    tournament.name = Concat(Value(QUOTE_BREAKING_TEXT), F("name"))
    tournament.desc = "plain"
    async with capture_queries() as counter:
        await tournament.save()
    _assert_not_in_sql(counter.queries, QUOTE_BREAKING_TEXT)
    stored = await Tournament.objects.get(id=1)
    assert stored.name == f"{QUOTE_BREAKING_TEXT}alpha{SECRET_TOKEN}"
    assert stored.desc == "plain"


@pytest.mark.asyncio
async def test_save_binds_an_arithmetic_literal_with_update_fields(db):
    """An IntField SET from an expression is read back and re-validated on SQLite."""
    row = await IntFields.objects.create(id=1, intnum=10)
    row.intnum = F("intnum") + 7
    async with capture_queries() as counter:
        await row.save(update_fields=["intnum"])
    assert not any("+7" in query for query in counter.queries), counter.queries
    assert (await IntFields.objects.get(id=1)).intnum == 17


@pytest.mark.asyncio
async def test_save_expression_with_optimistic_lock_field_binds_in_placeholder_order(db):
    target = await UpsertTarget.objects.create(code="a", note="n")
    target.note = Concat(F("note"), Value(SECRET_TOKEN))
    await target.save()
    stored = await UpsertTarget.objects.get(pk=target.pk)
    assert stored.note == f"n{SECRET_TOKEN}"
    assert stored.version == target.version == 1


@pytest.mark.asyncio
async def test_save_expression_with_tenant_guard_binds_in_placeholder_order(db):
    with Tenancy.scope(1):
        author = await TenantActiveAuthor.objects.create(name="author", company_id=1)
        author.name = Concat(Value(SECRET_TOKEN), F("name"))
        async with capture_queries() as counter:
            await author.save()
        _assert_not_in_sql(counter.queries, SECRET_TOKEN)
        assert (await TenantActiveAuthor.objects.get(pk=author.pk)).name == f"{SECRET_TOKEN}author"


class BoundEnum(StrEnum):
    marker = "ENUM-VALUE-4d2e"


JSON_KEY_WITH_QUOTES = "k'ey\\"
JSON_INJECTION_KEY = "x\\')IS NULL OR TRUE OR (\"data\"->'x"


@asynccontextmanager
async def standard_conforming_strings_off():
    """A transaction whose Postgres session reads backslashes in ``'...'`` literals as escapes."""
    async with Transactions.atomic() as connection:
        await connection.execute_script("SET LOCAL standard_conforming_strings = off")
        rows = await connection.execute_dicts("SHOW standard_conforming_strings")
        assert rows == [{"standard_conforming_strings": "off"}]
        yield connection
        await connection.execute_script("SET LOCAL standard_conforming_strings = on")


async def _upsert_as_tenant(tenant: str, returning: bool) -> list[str]:
    await SlugTenantAccount.objects.create(id=1, tenant="other", sku="SKU-1", balance=100)
    await SlugTenantAccount.objects.create(id=2, tenant=tenant, sku="SKU-2", balance=100)
    with Tenancy.scope(tenant):
        async with capture_queries() as counter:
            await SlugTenantAccount.objects.bulk_create(
                [
                    SlugTenantAccount(id=3, tenant=tenant, sku="SKU-1", balance=5),
                    SlugTenantAccount(id=4, tenant=tenant, sku="SKU-2", balance=7),
                    SlugTenantAccount(id=5, tenant=tenant, sku="SKU-3", balance=9),
                ],
                update_fields=["balance"],
                on_conflict=["sku"],
                returning=returning,
            )
    return counter.queries


async def _get_accounts() -> list[tuple[str, str, int]]:
    return [
        (account.sku, account.tenant, account.balance)
        for account in await SlugTenantAccount.objects.all_tenants().order_by("sku")
    ]


def _get_expected_accounts(tenant: str) -> list[tuple[str, str, int]]:
    return [("SKU-1", "other", 100), ("SKU-2", tenant, 7), ("SKU-3", tenant, 9)]


@requires_features(supports_unique_constraints=True)
@pytest.mark.parametrize("returning", [False, True])
@pytest.mark.asyncio
async def test_bulk_create_upsert_binds_the_active_tenant(db, returning):
    """The single-row execute_many() template and the multi-row statement both bind the tenant
    their ON CONFLICT ... DO UPDATE is limited to."""
    if returning and SlugTenantAccount._meta.connection.dialect.name == "sqlite":
        pytest.skip("bulk_create(returning=True) is Postgres-only")
    tenant = f"{SECRET_TOKEN}{QUOTE_BREAKING_TEXT}"
    queries = await _upsert_as_tenant(tenant, returning)

    _assert_not_in_sql(queries, SECRET_TOKEN)
    assert await _get_accounts() == _get_expected_accounts(tenant)


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_bulk_create_upsert_sql_shows_the_tenant_as_a_placeholder(db):
    with Tenancy.scope(SECRET_TOKEN):
        sql = SlugTenantAccount.objects.bulk_create(
            [SlugTenantAccount(id=1, tenant=SECRET_TOKEN, sku="SKU-1")], update_fields=["balance"], on_conflict=["sku"]
        ).sql()
    placeholder = "$5" if SlugTenantAccount._meta.connection.dialect.name == "postgresql" else "?"
    assert SECRET_TOKEN not in sql
    assert sql.endswith(f'WHERE "slugtenantaccount"."tenant"={placeholder}')


@test.requires_features(dialect="postgresql")
@pytest.mark.parametrize("returning", [False, True])
@pytest.mark.asyncio
async def test_bulk_create_upsert_tenant_scope_holds_without_standard_conforming_strings(db, returning):
    # A tenant of its own - a statement asyncpg already prepared with the same text would keep
    # the way it was parsed before standard_conforming_strings changed.
    tenant = f"scs-off-{returning}{QUOTE_BREAKING_TEXT}"
    async with standard_conforming_strings_off():
        queries = await _upsert_as_tenant(tenant, returning)
        accounts = await _get_accounts()

    _assert_not_in_sql(queries, f"scs-off-{returning}")
    assert accounts == _get_expected_accounts(tenant)


async def _create_json_rows(key: str) -> None:
    await JSONFields.objects.create(id=1, data={"color": "red", key: {SECRET_TOKEN: 5}, "tags": ["a", "b"]})
    await JSONFields.objects.create(id=2, data={"color": "blue", key: {SECRET_TOKEN: 6}, "tags": ["c"]})


async def _read_json_paths(key: str) -> tuple[list[int], list[tuple[object, object]]]:
    path = f"data__{key}__{SECRET_TOKEN}"
    filtered = await JSONFields.objects.annotate(picked=F(path)).filter(picked=6).values_list("id", flat=True)
    picked = (
        await JSONFields.objects.annotate(picked=F(path), last_tag=F("data__tags__-1"))
        .order_by("id")
        .values_list("picked", "last_tag")
    )
    return filtered, picked


@pytest.mark.asyncio
async def test_json_path_keys_with_quotes_and_backslashes_read_back(db):
    await _create_json_rows(JSON_KEY_WITH_QUOTES)

    assert await _read_json_paths(JSON_KEY_WITH_QUOTES) == ([2], [(5, "b"), (6, "c")])


async def _filter_by_json_keys(key: str, injection_key: str) -> tuple[list[int], ...]:
    by_value = await JSONFields.objects.filter(data__filter={f"{key}__{SECRET_TOKEN}__gte": 6}).values_list(
        "id", flat=True
    )
    by_index = await JSONFields.objects.filter(data__filter={"tags__-1": "b"}).values_list("id", flat=True)
    by_key = (
        await JSONFields.objects.filter(data__filter={f"{key}__has_key": SECRET_TOKEN})
        .order_by("id")
        .values_list("id", flat=True)
    )
    injected = await JSONFields.objects.filter(id=1, data__filter={injection_key: "zz"}).values_list("id", flat=True)
    return by_value, by_index, by_key, injected


@test.requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_json_filter_keys_with_quotes_and_backslashes_match(db):
    await _create_json_rows(JSON_KEY_WITH_QUOTES)

    assert await _filter_by_json_keys(JSON_KEY_WITH_QUOTES, JSON_INJECTION_KEY) == ([2], [1], [1, 2], [])


@test.requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_json_path_keys_read_the_same_without_standard_conforming_strings(db):
    """A key literal is an escape string (E'...') once it holds a backslash - a key crafted to
    close the literal under standard_conforming_strings=off stays inside it."""
    # Keys of its own - a statement asyncpg already prepared with the same text would keep the
    # way it was parsed before standard_conforming_strings changed.
    key = f"scs-off-{JSON_KEY_WITH_QUOTES}"
    await _create_json_rows(key)

    async with standard_conforming_strings_off():
        paths = await _read_json_paths(key)
        filters = await _filter_by_json_keys(key, f"scs-off-{JSON_INJECTION_KEY}")

    assert paths == ([2], [(5, "b"), (6, "c")])
    assert filters == ([2], [1], [1, 2], [])


@test.requires_features(dialect="postgresql")
@pytest.mark.parametrize("key", ["color", "k'e\\y"])
@pytest.mark.asyncio
async def test_json_path_expression_index_is_used_on_postgres(db, key):
    """A JSON path key stays a literal - an index on the same path expression, written with a
    plain '...' literal, serves the query, a generic plan of a statement run many times included."""
    await JSONFields.objects.create(id=1, data={key: "red"})
    await JSONFields.objects.create(id=2, data={key: "blue"})
    key_literal = "'" + key.replace("'", "''") + "'"
    queryset = (
        JSONFields.objects.annotate(value=F(f"data__{key}")).filter(value="red")._get_compiler()._get_execution_query()
    )
    queryset._make_query()
    sql, values = queryset.query.get_parameterized_sql()
    arguments_sql = ", ".join(POSTGRESQL_DIALECT.literals.get_string_literal_sql(str(value)) for value in values)
    async with Transactions.atomic() as connection:
        await connection.execute_script(f"CREATE INDEX ck_json_path_index ON jsonfields ((data->{key_literal}))")
        await connection.execute_script("SET LOCAL enable_seqscan = off")
        # asyncpg reuses one prepared statement for the same SQL text - Postgres may switch it to a
        # generic plan after five runs.
        for _ in range(7):
            assert await JSONFields.objects.annotate(value=F(f"data__{key}")).filter(value="red").values_list(
                "id", flat=True
            ) == [1]
        custom_plan = await JSONFields.objects.annotate(value=F(f"data__{key}")).filter(value="red").explain()
        await connection.execute_script(f"PREPARE ck_json_path_statement AS {sql}")
        await connection.execute_script("SET LOCAL plan_cache_mode = force_generic_plan")
        generic_plan_rows = await connection.execute_dicts(f"EXPLAIN EXECUTE ck_json_path_statement({arguments_sql})")
        await connection.execute_script("DEALLOCATE ck_json_path_statement")
        await connection.execute_script("SET LOCAL plan_cache_mode = auto")
        await connection.execute_script("SET LOCAL enable_seqscan = on")

    generic_plan = " ".join(str(row["QUERY PLAN"]) for row in generic_plan_rows)
    assert "ck_json_path_index" in str(custom_plan)
    assert "ck_json_path_index" in generic_plan, generic_plan


@test.requires_features(dialect="sqlite")
@pytest.mark.parametrize("key", ["color", "k'e\\y"])
@pytest.mark.asyncio
async def test_json_path_expression_index_is_used_on_sqlite(db, key):
    await JSONFields.objects.create(id=1, data={key: "red"})
    await JSONFields.objects.create(id=2, data={key: "blue"})
    path_sql = JSONAttributeCriterion(Field("data"), [key], as_text=False).get_sql(
        DEFAULT_SQL_CONTEXT.copy(dialect=SQLITE_DIALECT)
    )
    async with Transactions.atomic() as connection:
        await connection.execute_script(f"CREATE INDEX ck_json_path_index ON jsonfields ({path_sql})")
        queryset = JSONFields.objects.annotate(value=F(f"data__{key}")).filter(value="red")
        assert await queryset.values_list("id", flat=True) == [1]
        plan = await JSONFields.objects.annotate(value=F(f"data__{key}")).filter(value="red").explain()

    assert any("ck_json_path_index" in str(tuple(row)) for row in plan), [tuple(row) for row in plan]


@pytest.mark.asyncio
async def test_enum_filter_value_is_bound(db):
    await CharFields.objects.create(char=BoundEnum.marker.value)
    await CharFields.objects.create(char="other")

    async with capture_queries() as counter:
        matched = await CharFields.objects.filter(char=BoundEnum.marker).values_list("char", flat=True)
        excluded = await CharFields.objects.exclude(char=BoundEnum.marker).values_list("char", flat=True)

    _assert_not_in_sql(counter.queries, BoundEnum.marker.value)
    assert (matched, excluded) == ([BoundEnum.marker.value], ["other"])


@test.requires_features(dialect="postgresql")
@pytest.mark.parametrize("standard_conforming_strings", ["on", "off"])
@pytest.mark.asyncio
async def test_utility_statement_string_literal_reads_back_unchanged(db, standard_conforming_strings):
    """PREPARE TRANSACTION/COMMIT PREPARED take no bind parameter - their GID literal reads the same
    whatever standard_conforming_strings is."""
    value = "gid\\' OR 1=1 --\\\\x'y"
    async with Transactions.atomic() as connection:
        await connection.execute_script(f"SET LOCAL standard_conforming_strings = {standard_conforming_strings}")
        rows = await connection.execute_dicts(
            f"SELECT {connection.dialect.literals.get_string_literal_sql(value)} AS value"
        )
        await connection.execute_script("SET LOCAL standard_conforming_strings = on")
    assert rows == [{"value": value}]


class BoundIntEnum(IntEnum):
    high = 300


@pytest.mark.asyncio
async def test_enum_literal_selects_as_its_value(db):
    """An IntEnum literal in SELECT got no cast on Postgres (the cast was looked up by the enum's
    own type) and asyncpg refused to bind its int value to the untyped parameter."""
    await IntFields.objects.create(intnum=1)
    await IntFields.objects.create(intnum=2)

    assert await IntFields.objects.filter(intnum=1).annotate(level=Value(BoundIntEnum.high)).values_list(
        "level", flat=True
    ) == [300]
    assert await IntFields.objects.filter(intnum=1).values_list(level=Value(BoundIntEnum.high)) == [(300,)]
    assert await IntFields.objects.all().values(level=Value(BoundIntEnum.high)).annotate(n=Count("id")).values_list(
        "level", "n"
    ) == [(300, 2)]
    assert await IntFields.objects.all().aggregate(most=Max(Value(BoundIntEnum.high))) == {"most": 300}
    assert await IntFields.objects.filter(intnum=1).annotate(total=F("intnum") + Value(BoundIntEnum.high)).values_list(
        "total", flat=True
    ) == [301]
    assert await IntFields.objects.filter(intnum=1).annotate(
        level=Coalesce("intnum_null", Value(BoundIntEnum.high))
    ).values_list("level", flat=True) == [300]
    assert await IntFields.objects.filter(intnum=1).annotate(marker=Value(BoundEnum.marker)).values_list(
        "marker", flat=True
    ) == [BoundEnum.marker.value]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_null_byte_in_a_name_written_into_the_sql_is_rejected(db):
    """A null byte in an output name or a JSON path key reached the statement text - asyncpg failed
    with a protocol error classified as DBConnectionError, rust_pg with OperationalError."""
    await IntFields.objects.create(intnum=1)
    await JSONFields.objects.create(id=1, data={"color": "red"})
    null_byte_name = "a\x00b"

    with pytest.raises(ValidationError, match="null byte"):
        await IntFields.objects.all().values(**{null_byte_name: F("id")})
    with pytest.raises(ValidationError, match="null byte"):
        await IntFields.objects.all().values("intnum").annotate(n=Count("id")).values(**{null_byte_name: "n"})
    with pytest.raises(ValidationError, match="null byte"):
        await JSONFields.objects.annotate(color=F("data__co\x00lor")).values_list("color", flat=True)
    with pytest.raises(ValidationError, match="null byte"):
        await JSONFields.objects.filter(id=1).update(data_null=F("data__co\x00lor"))
    async with Transactions.atomic():
        with pytest.raises(ValidationError, match="null byte"):
            await JSONFields.objects.annotate(color=F("data__co\x00lor")).values_list("color", flat=True)
        # Rejected before it was sent, so the transaction is still usable.
        assert await JSONFields.objects.all().count() == 1


@pytest.mark.asyncio
@test.requires_features(dialect="postgresql")
async def test_null_byte_in_postgres_statement_text_is_rejected(db):
    """A null byte in a JSON __filter key or a raw SQL text broke the Postgres protocol message."""
    await JSONFields.objects.create(id=1, data={"color": "red"})

    with pytest.raises(ValidationError, match="null byte"):
        await JSONFields.objects.filter(data__filter={"co\x00lor": "red"})
    with pytest.raises(ValidationError, match="null byte"):
        await JSONFields.objects.annotate(raw=RawSQL("'a\x00'", [])).values_list("raw", flat=True)
    connection = Connections.get("models")
    with pytest.raises(ValidationError, match="null byte"):
        await connection.execute("SELECT '\x00'")
    async with Transactions.atomic():
        with pytest.raises(ValidationError, match="null byte"):
            await JSONFields.objects.filter(data__filter={"co\x00lor": "red"})
        assert await JSONFields.objects.all().count() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "lookup", ["contains", "icontains", "startswith", "istartswith", "endswith", "iendswith", "iexact"]
)
async def test_null_byte_in_a_like_lookup_value_is_rejected(db, lookup):
    """SQLite cut a LIKE pattern at the null byte - '%\x00x%' matched every row - and Postgres
    failed on it; every backend now refuses the value, the way an equality filter does."""
    await Tournament.objects.create(id=1, name="alpha")
    await Tournament.objects.create(id=2, name="beta")
    # The same query shape again reuses the cached SQL and rebuilds only the pattern.
    assert await Tournament.objects.filter(**{f"name__{lookup}": "alpha"}).count() == 1
    with pytest.raises(ValidationError, match="null byte"):
        await Tournament.objects.filter(**{f"name__{lookup}": "\x00zzz"}).count()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name", ['x"; DROP TABLE tournament; --', "x y", "x;y", "x'y", "x`y", "x[y]", "x--y", "x/*y*/"]
)
async def test_unsafe_annotation_names_are_rejected(db, name):
    await Tournament.objects.create(id=1, name="alpha")
    for make_query in (
        lambda: Tournament.objects.annotate(**{name: F("id")}),
        lambda: Tournament.objects.all().alias(**{name: F("id")}),
        lambda: Tournament.objects.all().values(**{name: F("id")}),
        lambda: Tournament.objects.all().values_list(**{name: F("id")}),
        lambda: Tournament.objects.all().aggregate(**{name: Max("id")}),
    ):
        with pytest.raises(ValueError, match="can't contain whitespace, quotation marks"):
            await make_query()
    assert await Tournament.objects.all().count() == 1
