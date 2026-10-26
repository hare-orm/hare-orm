"""How ClickHouse writes rows: the option of lightweight ``UPDATE`` statements checked; a connection with
``async_insert`` has the server gather the inserted rows; what ClickHouse has no SQL of - ``RETURNING``,
``ON CONFLICT``, a sliced ``UPDATE`` - refused before any SQL."""

import datetime
import decimal
import ipaddress
import os

import pytest

from hare import Connections
from hare.contrib.test import assert_query_count, capture_queries
from hare.dialects.base.connection.db_url_config_generator import DbUrlConfigGenerator
from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
from hare.dialects.clickhouse.constants import CLICKHOUSE_DIALECT
from hare.exceptions import (
    ConfigurationError,
    FieldError,
    IntegrityError,
    OperationalError,
    QueryError,
    UnSupportedError,
    ValidationError,
)
from hare.instrumentation.queries.query_tags import QueryTags
from hare.models.tenancy.tenancy import Tenancy
from hare.query.expressions import F, OuterReference, Subquery, Value
from hare.query.functions import Sum
from tests.dialects.clickhouse.models import (
    Account,
    Country,
    Device,
    DeviceLevel,
    DeviceStatus,
    EventLog,
    NetworkHost,
    NetworkRoute,
    PageView,
    Passport,
    Player,
    Reading,
    Revision,
    Team,
    TenantDocument,
    TenantLabel,
    TenantNote,
)
from tests.utils.database_under_test import DatabaseUnderTest


@pytest.mark.database_independent
def test_the_option_is_checked():
    with pytest.raises(ConfigurationError, match="MergeTree family"):
        ClickhouseTableOptions(engine="Memory", lightweight_updates=True).raise_if_unsupported(
            Account, CLICKHOUSE_DIALECT.features
        )


@pytest.mark.asyncio
async def test_a_connection_inserts_asynchronously(clickhouse_db):
    connection = Team._meta.connection
    url = os.environ["HARE_TEST_DB"]
    config = DbUrlConfigGenerator.expand(
        DatabaseUnderTest.get_url_with_options("async_insert=true", url.replace("{}", "x"))
    )
    assert config["credentials"]["async_insert"] is True
    credentials = {**config["credentials"], "database": connection.database}
    client = type(connection)(connection_alias="async_inserts", **credentials)
    assert client.get_session_settings()["async_insert"] == 1
    assert client.get_session_settings()["wait_for_async_insert"] == 1
    try:
        await client.execute_script("INSERT INTO account (id, owner, balance) VALUES (100, 'z', 1)")
        await client.execute_script("INSERT INTO account (id, owner, balance) VALUES (101, 'z', 2)")
        # Waited for: the rows are written when the statement returns.
        assert await Account.objects.filter(owner="z").count() == 2
        await client.execute_dicts("SELECT 1")
        rows = await client.execute_dicts(
            "SELECT value FROM system.settings WHERE name IN ('async_insert', 'wait_for_async_insert') ORDER BY name"
        )
        assert [row["value"] for row in rows] == ["1", "1"]
    finally:
        await client.close()
    not_waiting = type(connection)(connection_alias="async", **{**credentials, "wait_for_async_insert": False})
    assert not_waiting.get_session_settings()["wait_for_async_insert"] == 0
    with pytest.raises(ConfigurationError):
        type(connection)(connection_alias="async", **{**credentials, "async_insert": "sometimes"})


@pytest.mark.asyncio
async def test_a_write_returning_its_rows_is_refused(clickhouse_db):
    async with assert_query_count(0):
        for refused in (
            lambda: Account.objects.filter(owner="a").update(balance=50).returning("id", "balance"),
            lambda: Account.objects.filter(owner="b").delete().returning("id"),
            lambda: Account.objects.bulk_create([Account(id=1, owner="a")], returning=True),
        ):
            with pytest.raises(UnSupportedError, match="RETURNING"):
                await refused()


@pytest.mark.asyncio
async def test_an_insert_handling_conflicts_is_refused(clickhouse_db):
    async with assert_query_count(0):
        for refused in (
            lambda: Account.objects.bulk_create([Account(id=1, owner="a")], ignore_conflicts=True),
            lambda: Account.objects.bulk_create(
                [Account(id=1, owner="a")], on_conflict=["id"], update_fields=["owner"]
            ),
            lambda: Account.objects.bulk_create(
                [Account(id=1, owner="a")], on_conflict_constraint="pk", update_fields=["owner"]
            ),
        ):
            with pytest.raises(UnSupportedError, match="ON CONFLICT"):
                await refused()


@pytest.mark.asyncio
async def test_a_write_of_stored_rows_reports_no_count(clickhouse_db):
    """A mutation reports no count of the rows it changed - the count is None, as the server gives it."""
    await Account.objects.bulk_create([Account(id=number, owner="a", balance=number) for number in range(3)])
    assert await Account.objects.filter(id__lt=2).update(balance=7) is None
    accounts = await Account.objects.order_by("id")
    for account in accounts:
        account.owner = "b"
    assert await Account.objects.bulk_update(accounts, ["owner"]) is None
    assert await Account.objects.filter(id=0).delete() is None
    assert await Account.objects.order_by("id").values_list("id", "owner", "balance") == [(1, "b", 7), (2, "b", 2)]


@pytest.mark.asyncio
async def test_an_update_whose_condition_folds_into_a_constant_runs(clickhouse_db):
    # NOT of a non-Nullable column's IS NULL is a constant.
    if (await Account._meta.connection.get_server_version())[:2] < (26, 3):
        pytest.skip(
            "ClickHouse before 26.3 refuses an UPDATE of a constant value under a negation folding into a constant"
        )
    await Account.objects.bulk_create([Account(id=number, owner="a", balance=number) for number in range(3)])
    await Account.objects.exclude(owner__isnull=True).update(balance=0)
    assert await Account.objects.values_list("balance", flat=True) == [0, 0, 0]


@pytest.mark.asyncio
async def test_a_sliced_write_is_refused(clickhouse_db):
    async with assert_query_count(0):
        for refused in (
            lambda: Account.objects.all()[:1].update(owner="b"),
            lambda: Account.objects.filter(owner="a")[1:].update(balance=5),
            lambda: Account.objects.all()[:2].delete(),
        ):
            with pytest.raises(QueryError, match="slice"):
                await refused()


@pytest.mark.asyncio
async def test_an_insert_from_a_queryset_writes_its_rows(clickhouse_db):
    await Account.objects.bulk_create([Account(id=number, owner="a") for number in range(2)])
    shifted = Account.objects.annotate(new_id=F("id") + 10).values_list("new_id", "owner")
    assert await Account.objects.insert_from(shifted, fields=["id", "owner"]) == 2
    assert await Account.objects.order_by("id").values_list("id", flat=True) == [0, 1, 10, 11]


@pytest.mark.asyncio
async def test_a_key_column_is_left_as_it_is_and_refused_when_changed(clickhouse_db):
    assert PageView._meta.get_table_options(CLICKHOUSE_DIALECT).get_fixed_field_names(PageView) == {
        "site",
        "viewed_at",
    }
    assert Reading._meta.get_table_options(CLICKHOUSE_DIALECT).get_fixed_field_names(Reading) == {"version"}
    moment = datetime.datetime(2026, 1, 2, tzinfo=datetime.UTC)
    view = await PageView.objects.create(site="home", viewed_at=moment, duration_ms=5)
    reading = await Reading.objects.create(id=1, sensor="t", value=3, version=1)
    # A save writes the other columns and leaves the keys, unchanged, as they are.
    view.duration_ms = 9
    await view.save()
    reading.value = 4
    await reading.save()
    assert await PageView.objects.values_list("site", "duration_ms") == [("home", 9)]
    assert await Reading.objects.values_list("value", "version") == [(4, 1)]
    view.site = "about"
    with pytest.raises(UnSupportedError, match="site"):
        await view.save()
    with pytest.raises(UnSupportedError, match="site"):
        await view.save(update_fields=["site"])
    with pytest.raises(UnSupportedError, match="version"):
        await Reading.objects.filter(id=1).update(version=2)
    with pytest.raises(UnSupportedError, match="viewed_at"):
        await PageView.objects.bulk_update([view], ["viewed_at"])
    assert await PageView.objects.values_list("site", flat=True) == ["home"]


@pytest.mark.asyncio
async def test_a_part_of_a_composite_primary_key_is_refused_when_written(clickhouse_db):
    """The sorting key begins with the primary key - an update of one of its columns came back as the
    server's own error, and a correlated subquery an UPDATE sets a column to wasn't looked for."""
    await Revision.objects.bulk_create(
        [Revision(document_id=1, number=1, title="a", score=1.0), Revision(document_id=1, number=2, title="b")]
    )
    with pytest.raises(UnSupportedError, match="Revision: number - a column of the table's keys"):
        await Revision.objects.filter(pk=(1, 1)).update(number=50)
    with pytest.raises(UnSupportedError, match="Revision: document_id"):
        await Revision.objects.filter(pk=(1, 1)).update(document_id=F("document_id") + 1)
    scores = Revision.objects.filter(pk=OuterReference("pk")).values("score")
    with pytest.raises(UnSupportedError, match="correlated"):
        await Revision.objects.update(score=Subquery(scores))
    assert sorted(await Revision.objects.values_list("document_id", "number", "score")) == [(1, 1, 1.0), (1, 2, None)]
    await Revision.objects.filter(pk=(1, 1)).update(title="c")
    assert await Revision.objects.filter(pk=(1, 1)).values_list("title", flat=True) == ["c"]


@pytest.mark.asyncio
async def test_rows_of_a_table_without_a_primary_key_are_written(clickhouse_db):
    await EventLog.objects.create(source="ann", duration=5)
    await EventLog(source="bob").save()
    await EventLog.objects.bulk_create([EventLog(source="cid"), EventLog(source="ann")])
    assert await EventLog.objects.values_list("source", flat=True) == ["ann", "ann", "bob", "cid"]


@pytest.mark.asyncio
async def test_an_update_of_an_unknown_field_or_the_key_is_refused(clickhouse_db):
    with pytest.raises(FieldError, match="Unknown field 'nope'"):
        await Account.objects.filter(id=1).update(nope=1)
    with pytest.raises(QueryError, match="Cannot set 'pk' via .update"):
        await Account.objects.filter(id=1).update(pk=2)


@pytest.mark.asyncio
async def test_a_bulk_update_finds_rows_by_keys_narrower_than_other_rows_keys(clickhouse_db):
    """The keys of the updated rows make a set: of the literals 1 and 2 one of UInt8, and the server
    refused to look the key of another table row - 100000 - up in it."""
    accounts = [Account(id=key, owner=f"o{key}") for key in (1, 2, 100_000, -5, 2**40)]
    await Account.objects.bulk_create(accounts)
    revisions = [
        Revision(document_id=document_id, number=number, title="t")
        for document_id, number in ((1, 1), (1, 70_000), (-3, 2), (2**31 - 1, 3))
    ]
    await Revision.objects.bulk_create(revisions)

    for account in accounts[:2]:
        account.balance = account.id * 10
    await Account.objects.bulk_update(accounts[:2], ["balance"])
    revisions[0].title = "first"
    await Revision.objects.bulk_update(revisions[:1], ["title"])

    assert await Account.objects.order_by("id").values_list("id", "owner", "balance") == [
        (-5, "o-5", 0),
        (1, "o1", 10),
        (2, "o2", 20),
        (100_000, "o100000", 0),
        (2**40, f"o{2**40}", 0),
    ]
    assert await Revision.objects.order_by("document_id", "number").values_list("number", "title") == [
        (2, "t"),
        (1, "first"),
        (70_000, "t"),
        (3, "t"),
    ]


@pytest.mark.asyncio
async def test_a_bulk_update_writes_a_limited_number_of_rows_per_mutation(clickhouse_db):
    """One mutation of every row found each row by scanning the whole list, repeated in its condition
    and its assignments - its cost grew with the square of the rows, and 20 000 rows broke the
    server's AST limit."""
    accounts = [Account(id=key, owner=f"o{key}") for key in range(1, 2101)]
    await Account.objects.bulk_create(accounts)
    for account in accounts:
        account.balance = account.id * 2
    async with capture_queries() as queries:
        await Account.objects.bulk_update(accounts, ["balance"])
    mutations = [sql for sql in queries.queries if "UPDATE" in sql and sql.lstrip().upper().startswith("ALTER")]
    assert len(mutations) == 3
    assert await Account.objects.aggregate(total=Sum("balance")) == {"total": 2100 * 2101}


@pytest.mark.asyncio
async def test_an_integer_set_from_a_fraction_loses_the_fraction(clickhouse_db):
    """As ClickHouse converts - the fraction cut off; accurateCast() refused it."""
    moment = datetime.datetime(2024, 1, 2, tzinfo=datetime.UTC)
    views = [PageView(site="a", viewed_at=moment, duration_ms=duration) for duration in (1, 3)]
    await PageView.objects.bulk_create(views)

    await PageView.objects.filter(site="a").update(duration_ms=F("duration_ms") * decimal.Decimal("1.5"))
    assert sorted(await PageView.objects.values_list("duration_ms", flat=True)) == [1, 4]
    await PageView.objects.filter(site="a").update(duration_ms=F("duration_ms") * 2.5)
    assert sorted(await PageView.objects.values_list("duration_ms", flat=True)) == [2, 10]


@pytest.mark.asyncio
async def test_a_moment_shifted_beyond_the_stored_ones_is_computed_by_clickhouse(clickhouse_db):
    """ClickHouse's own addMicroseconds() - hare checks nothing; a moment read is computed past the
    years a column stores."""
    await PageView.objects.create(site="a", viewed_at=datetime.datetime(2299, 12, 1, tzinfo=datetime.UTC))

    shifted = await PageView.objects.annotate(
        later=F("viewed_at") + datetime.timedelta(days=60),
        earlier=F("viewed_at") - datetime.timedelta(days=200_000),
    ).values_list("later", "earlier")
    assert shifted == [
        (
            datetime.datetime(2300, 1, 30, tzinfo=datetime.UTC),
            datetime.datetime(2299, 12, 1, tzinfo=datetime.UTC) - datetime.timedelta(days=200_000),
        )
    ]


@pytest.mark.asyncio
async def test_a_row_of_a_tenant_is_saved_under_its_scope(clickhouse_db):
    # The scope's guard casts its parameter to the column's type, as the server spells it (Int32).
    with Tenancy.scope(1):
        document = await TenantDocument.objects.create(id=21, title="a")
        document.title = "b"
        await document.save()
        assert await TenantDocument.objects.filter(id=21).values_list("title", flat=True) == ["b"]


@pytest.mark.asyncio
async def test_a_row_is_inserted_under_query_tags(clickhouse_db):
    """The tags' comment ended the INSERT, after its VALUES - ClickHouse read it as data ("Cannot
    parse input: expected '('"); every create() under QueryTags.scope() failed."""
    async with capture_queries() as captured:
        with QueryTags.scope(application="billing"):
            await Account.objects.create(id=7, owner="a", balance=1)
            await Account.objects.get_or_create(id=8, defaults={"owner": "b", "balance": 2})
    assert await Account.objects.order_by("id").values_list("id", flat=True) == [7, 8]
    inserts = [sql for sql in captured.queries if "INSERT INTO" in sql]
    assert inserts and all(sql.startswith("/*application='billing'*/ INSERT") for sql in inserts)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "leading_comment", ["-- migration step\n", "/* bulk load */ ", "  /* a */ -- b\n  "], ids=["line", "block", "both"]
)
async def test_a_raw_insert_starting_with_a_comment_is_run_under_query_tags(clickhouse_db, leading_comment):
    """An INSERT starting with a comment took the tags at its end - ClickHouse read them as data
    ("Cannot parse input: expected '('")."""
    client = Connections.get("models")
    with QueryTags.scope(application="billing"):
        await client.execute_script(f"{leading_comment}INSERT INTO account (id, owner, balance) VALUES (300, 'c', 3)")
    assert await Account.objects.filter(id=300).count() == 1


@pytest.mark.asyncio
async def test_a_save_under_every_tenant_writes_the_row(clickhouse_db):
    """The guard of a write under every tenant checks a NULL tenant value - cast to the tenant column's
    type, which holds no NULL, the server refused it (code 70)."""
    with Tenancy.scope(Tenancy.ALL):
        document = await TenantDocument.objects.create(id=1, title="a", tenant_id=7)
        document.title = "b"
        await document.save()
        assert await TenantDocument.objects.filter(id=1).values_list("title", flat=True) == ["b"]


@pytest.mark.asyncio
async def test_null_written_into_a_not_null_column_is_an_integrity_error(clickhouse_db):
    """ClickHouse's "Cannot convert NULL to a non-nullable type" (code 70) came out as a bare
    OperationalError - an IntegrityError on every other database."""
    player = await Player.objects.create(id=1, name="p")
    player.active = None
    for refused in (
        player.save,
        lambda: Player.objects.bulk_update([player], fields=["active"]),
        lambda: Player.objects.filter(id=1).update(active=Value(None)),
    ):
        with pytest.raises(IntegrityError, match="Cannot convert NULL"):
            await refused()
    assert await Player.objects.values_list("active", flat=True) == [True]


@pytest.mark.parametrize(
    ("stored", "written"),
    [("2001:db8::1", "2001:DB8::1"), ("2001:db8::1", "2001:0db8:0:0:0:0:0:1"), ("::ffff:1.2.3.4", "::ffff:102:304")],
)
@pytest.mark.asyncio
async def test_another_text_of_a_stored_address_is_the_same_value(clickhouse_db, stored, written):
    """An address written in another text is found by the stored one - as an address, not as a text."""
    await NetworkHost.objects.create(id=1, address=stored, port=80)
    await NetworkRoute.objects.create(address=stored, hop=1)
    assert await NetworkHost.objects.filter(address=written).values_list("id", flat=True) == [1]
    assert await NetworkRoute.objects.filter(address=written).count() == 1


@pytest.mark.asyncio
async def test_a_null_byte_refusal_names_no_value(clickhouse_db):
    """The refusal of a text holding a null byte quoted the text - a sensitive field's value went into
    the error and the logs."""
    for refused in (
        lambda: Passport.objects.create(code="top-secret\x00"),
        lambda: Passport.objects.filter(code="top-secret\x00").count(),
    ):
        with pytest.raises(ValidationError, match="null byte") as error:
            await refused()
        assert "top-secret" not in str(error.value)


@pytest.mark.asyncio
async def test_integer_arithmetic_set_past_the_column_type_wraps_around_as_clickhouse_writes_it(
    clickhouse_db,
):
    """A mutation is ClickHouse's own - an integer past its column's type wraps around."""
    moment = datetime.datetime(2024, 1, 2, tzinfo=datetime.UTC)
    await PageView.objects.create(site="a", viewed_at=moment, duration_ms=2**31 - 1)
    await Country.objects.create(code="fr", name="France", population=2**63 - 1)

    await PageView.objects.filter(site="a").update(duration_ms=F("duration_ms") + 1)
    await Country.objects.filter(code="fr").update(population=F("population") + 1)
    assert await PageView.objects.values_list("duration_ms", flat=True) == [-(2**31)]
    assert await Country.objects.values_list("population", flat=True) == [-(2**63)]


@pytest.mark.asyncio
async def test_a_field_set_to_a_subquery_writes_its_value(clickhouse_db):
    """A subquery is a query's expression and an SQL term at once - an update to a bare Subquery was
    refused, the subquery bound as a parameter. A value past the column's type wraps around, as a
    mutation of ClickHouse writes it."""
    if (await PageView._meta.connection.get_server_version())[:2] >= (26, 7):
        pytest.skip(
            "ClickHouse 26.7 refuses a mutation setting a column without NULL to a Nullable subquery "
            "(CANNOT_INSERT_NULL_IN_ORDINARY_COLUMN) - the population column is Nullable"
        )
    moment = datetime.datetime(2024, 1, 2, tzinfo=datetime.UTC)
    await PageView.objects.create(site="a", viewed_at=moment, duration_ms=1)
    await Country.objects.create(code="fr", name="France", population=7)
    await Country.objects.create(code="de", name="Germany", population=2**40)

    def get_population(code: str) -> Subquery:
        return Subquery(Country.objects.filter(code=code).values("population")[:1])

    await PageView.objects.filter(site="a").update(duration_ms=get_population("fr"))
    assert await PageView.objects.values_list("duration_ms", flat=True) == [7]
    await PageView.objects.filter(site="a").update(duration_ms=get_population("de"))
    assert await PageView.objects.values_list("duration_ms", flat=True) == [0]


@pytest.mark.asyncio
async def test_integer_arithmetic_set_into_an_enum_writes_a_member_and_refuses_a_non_member(clickhouse_db):
    """Exact arithmetic is an Int256, which ClickHouse converts to no Enum16 - every arithmetic update of an
    integer enum failed, a member's number too."""
    await Device.objects.create(
        id=1,
        tiny=1,
        big=1,
        ratio=0.5,
        code="ab",
        country="fr",
        status=DeviceStatus.ONLINE,
        level=DeviceLevel.LOW,
        address=ipaddress.IPv4Address("10.0.0.7"),
        labels=[],
    )

    await Device.objects.filter(id=1).update(level=F("level") + 499)
    assert await Device.objects.values_list("level", flat=True) == [DeviceLevel.HIGH]
    with pytest.raises(OperationalError, match="no member of the enum"):
        await Device.objects.filter(id=1).update(level=F("level") + 1)
    assert await Device.objects.values_list("level", flat=True) == [DeviceLevel.HIGH]


@pytest.mark.asyncio
async def test_two_tenants_rows_of_one_key_are_told_apart_by_their_tenant(clickhouse_db):
    """ClickHouse keeps no key unique - a relation's check read another tenant's row of the key and refused,
    a delete's cascade reached another tenant's rows pointing at the key."""
    for tenant in (1, 2):
        with Tenancy.scope(tenant):
            await TenantDocument.objects.create(id=1, title=f"of {tenant}")
            await TenantLabel.objects.create(code="a", region="eu")
    with Tenancy.scope(2):
        await TenantNote.objects.create(id=2, document_id=1)
        document = await TenantDocument.objects.get(id=1)
        await document.labels.add(await TenantLabel.objects.get(code="a", region="eu"))
    with Tenancy.scope(1):
        await TenantNote.objects.create(id=1, document_id=1)
        await (await TenantDocument.objects.get(id=1)).delete()

    with Tenancy.scope(Tenancy.ALL):
        assert await TenantDocument.objects.values_list("title", flat=True) == ["of 2"]
        assert await TenantNote.objects.values_list("id", flat=True) == [2]


@pytest.mark.asyncio
async def test_a_json_document_written_by_a_mutation_equals_the_inserted_one(clickhouse_db):
    """A JSON column held an integer as an UInt64 written by an INSERT and as an Int64 by a mutation - the
    document an update wrote wasn't found by an exact filter."""
    document = {"a": 1, "b": [1, 2], "c": 2**40}
    await Player.objects.create(id=1, name="inserted", data=document)
    await Player.objects.create(id=2, name="updated")
    await Player.objects.filter(id=2).update(data=document)

    assert await Player.objects.filter(data=document).order_by("id").values_list("id", flat=True) == [1, 2]
    assert await Player.objects.filter(data__not=document).count() == 0


@pytest.mark.asyncio
async def test_a_json_boolean_beside_a_text_in_a_filter_stays_a_boolean(clickhouse_db):
    """The document a filter compared with held true beside a text as the text 'true' - a row holding the
    text was found."""
    await Player.objects.create(id=1, name="texts", data={"a": ["", "true"]})

    assert await Player.objects.filter(data={"a": ["", True]}).count() == 0
    assert await Player.objects.filter(data={"a": ["", "true"]}).count() == 1


@pytest.mark.asyncio
async def test_an_insert_from_refuses_a_literal_its_field_refuses(clickhouse_db):
    """A Value() the source selected was written unchecked - a text past max_length kept whole, an
    integer past its column's range wrapped around."""
    await Account.objects.create(id=1, owner="a")
    for literal, field_name in (("o" * 21, "owner"), (2**40, "balance")):
        source = Account.objects.annotate(new_id=Value(2), literal=Value(literal), kept=F("owner")).values_list(
            "new_id", "literal", "kept"
        )
        fields = ["id", field_name, "balance" if field_name == "owner" else "owner"]
        with pytest.raises(ValidationError, match=field_name):
            await Account.objects.insert_from(source, fields=fields)
    assert await Account.objects.count() == 1


@pytest.mark.asyncio
async def test_a_literal_beside_a_json_column_is_written_as_its_json(clickhouse_db):
    """A literal in Coalesce() beside a JSON column was bound as itself - a text that is no JSON was
    stored, and the row couldn't be read any more."""
    from hare.query.functions import Coalesce

    await Player.objects.create(id=1, name="p")
    for value, expected in (
        ({"a": 1}, {"a": 1}),
        ("text", "text"),
        ([None], [None]),
        (datetime.date(2024, 1, 2), "2024-01-02"),
    ):
        try:
            await Player.objects.filter(id=1).update(data=value)
        except ValidationError as error:
            # A JSON column of the server's JSON type holds an object alone - refused as a plain write is.
            for expression in (Coalesce(Value(value), F("data")), Coalesce(F("data"), Value(value))):
                with pytest.raises(ValidationError, match=str(error).split(":")[0]):
                    await Player.objects.filter(id=1).update(data=expression)
            continue
        await Player.objects.filter(id=1).update(data=None)
        for expression in (Coalesce(Value(value), F("data")), Coalesce(F("data"), Value(value))):
            await Player.objects.filter(id=1).update(data=expression)
            assert (await Player.objects.get(id=1)).data == expected
            await Player.objects.filter(id=1).update(data=None)
