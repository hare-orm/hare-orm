"""How ClickHouse writes rows: the option of lightweight ``UPDATE`` statements checked; a connection with
``async_insert`` has the server gather the inserted rows; the rows a write returns read by their keys."""

import datetime
import decimal
import os

import pytest

from hare.contrib.test import capture_queries
from hare.contrib.test.isolated_contexts import hare_test_context
from hare.dialects.base.connection.db_url_config_generator import DbUrlConfigGenerator
from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
from hare.dialects.clickhouse.constants import CLICKHOUSE_DIALECT
from hare.exceptions import ConfigurationError, FieldError, OperationalError, QueryError, UnSupportedError
from hare.models.tenancy.tenancy import Tenancy
from hare.query.expressions import F, OuterReference, Subquery
from hare.query.functions import Sum
from tests.dialects.clickhouse.models import Account, PageView, Reading, Revision, Team, TenantDocument
from tests.primary_keyless_models import Venue, VisitLog


def test_the_option_is_checked():
    features = CLICKHOUSE_DIALECT.features
    with pytest.raises(ConfigurationError):
        ClickhouseTableOptions(lightweight_updates="yes")
    with pytest.raises(ConfigurationError, match="MergeTree family"):
        ClickhouseTableOptions(engine="Memory", lightweight_updates=True).raise_if_unsupported(
            Account, features.replace(supports_lightweight_update=True)
        )
    with pytest.raises(UnSupportedError, match="25.7"):
        ClickhouseTableOptions(lightweight_updates=True).raise_if_unsupported(
            Account, features.replace(supports_lightweight_update=False)
        )


@pytest.mark.asyncio
async def test_a_table_altered_to_lightweight_updates_is_checked_as_a_created_one(clickhouse_db, monkeypatch):
    connection = Account._meta.connection
    monkeypatch.setattr(connection, "features", connection.features.replace(supports_lightweight_update=False))
    editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=True)
    with pytest.raises(UnSupportedError, match="25.7"):
        await editor.table_partitions.alter_table_options(
            Account, ClickhouseTableOptions(), ClickhouseTableOptions(lightweight_updates=True)
        )
    assert editor.collected_sql == []


@pytest.mark.asyncio
async def test_a_connection_inserts_asynchronously(clickhouse_db):
    connection = Team._meta.connection
    url = os.environ["HARE_TEST_DB"]
    config = DbUrlConfigGenerator.expand(f"{url.replace('{}', 'x')}?async_insert=true")
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
async def test_written_rows_are_returned_by_reading_them(clickhouse_db):
    await Account.objects.bulk_create(
        [Account(id=number, owner="ab"[number % 2], balance=number) for number in range(4)]
    )
    updated = await Account.objects.filter(owner="a").update(balance=50).returning("id", "balance")
    assert sorted(updated, key=lambda row: row["id"]) == [{"id": 0, "balance": 50}, {"id": 2, "balance": 50}]
    instances = await Account.objects.filter(id=1).update(owner="c").returning()
    assert [(account.id, account.owner) for account in instances] == [(1, "c")]
    deleted = await Account.objects.filter(owner="b").delete().returning("id", "balance")
    assert deleted == [{"id": 3, "balance": 3}]
    assert await Account.objects.order_by("id").values_list("id", flat=True) == [0, 1, 2]
    assert await Account.objects.filter(id=99).update(balance=1).returning() == []


@pytest.mark.asyncio
async def test_an_update_whose_condition_folds_into_a_constant_runs(clickhouse_db):
    # NOT of a non-Nullable column's IS NULL is a constant - ClickHouse 25.8 refuses an UPDATE of a constant
    # value under such a negation, so the condition is written as no negation.
    await Account.objects.bulk_create([Account(id=number, owner="a", balance=number) for number in range(3)])
    assert await Account.objects.exclude(owner__isnull=True).update(balance=0) == 3
    assert await Account.objects.values_list("balance", flat=True) == [0, 0, 0]


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
    if not Account._meta.connection.features.supports_generated_keys:
        pytest.skip("the venues' keys are taken from a series - the server keeps none")
    async with hare_test_context(["tests.primary_keyless_models"], db_url=os.environ["HARE_TEST_DB"]):
        hall = await Venue.objects.create(id=1, name="Hall")
        await VisitLog.objects.create(venue=hall, visitor="ann", duration=5)
        await VisitLog(venue=hall, visitor="bob").save()
        await VisitLog.objects.bulk_create([VisitLog(venue=hall, visitor="cid"), VisitLog(venue=hall, visitor="ann")])
        await VisitLog.objects.bulk_create([VisitLog(venue=hall, visitor="dan")], ignore_conflicts=True)
        assert await VisitLog.objects.values_list("visitor", flat=True) == ["ann", "ann", "bob", "cid", "dan"]


@pytest.mark.asyncio
async def test_an_update_of_an_unknown_field_or_the_key_is_refused(clickhouse_db):
    with pytest.raises(FieldError, match="Unknown keyword argument nope"):
        await Account.objects.filter(id=1).update(nope=1)
    with pytest.raises(QueryError, match="is PK and can not be updated"):
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
    assert await Account.objects.bulk_update(accounts[:2], ["balance"]) == 2
    revisions[0].title = "first"
    assert await Revision.objects.bulk_update(revisions[:1], ["title"]) == 1
    await Account.objects.bulk_create(
        [Account(id=2, owner="again"), Account(id=7, owner="new")], on_conflict=["id"], update_fields=["owner"]
    )

    assert await Account.objects.order_by("id").values_list("id", "owner", "balance") == [
        (-5, "o-5", 0),
        (1, "o1", 10),
        (2, "again", 20),
        (7, "new", 0),
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
        assert await Account.objects.bulk_update(accounts, ["balance"]) == 2100
    mutations = [sql for sql in queries.queries if "UPDATE" in sql and sql.lstrip().upper().startswith("ALTER")]
    assert len(mutations) == 3
    assert await Account.objects.aggregate(total=Sum("balance")) == {"total": 2100 * 2101}


@pytest.mark.asyncio
async def test_an_integer_set_from_a_fraction_is_rounded_as_postgres_rounds_it(clickhouse_db):
    """A Decimal's half away from zero, a float's to even - the fraction was refused."""
    moment = datetime.datetime(2024, 1, 2, tzinfo=datetime.UTC)
    views = [PageView(site="a", viewed_at=moment, duration_ms=duration) for duration in (1, 3)]
    await PageView.objects.bulk_create(views)

    await PageView.objects.filter(site="a").update(duration_ms=F("duration_ms") * decimal.Decimal("1.5"))
    assert sorted(await PageView.objects.values_list("duration_ms", flat=True)) == [2, 5]
    await PageView.objects.filter(site="a").update(duration_ms=F("duration_ms") * 2.5)
    assert sorted(await PageView.objects.values_list("duration_ms", flat=True)) == [5, 12]


@pytest.mark.asyncio
async def test_a_date_shifted_beyond_the_stored_ones_is_refused(clickhouse_db):
    """ClickHouse's own functions stop at the last date it stores."""
    await PageView.objects.create(site="a", viewed_at=datetime.datetime(2299, 12, 1, tzinfo=datetime.UTC))

    with pytest.raises(OperationalError, match="timestamp out of range"):
        await PageView.objects.annotate(later=F("viewed_at") + datetime.timedelta(days=60)).values_list("later")
    with pytest.raises(OperationalError, match="timestamp out of range"):
        await PageView.objects.annotate(earlier=F("viewed_at") - datetime.timedelta(days=200_000)).values_list(
            "earlier"
        )


@pytest.mark.asyncio
async def test_a_row_of_a_tenant_is_saved_under_its_scope(clickhouse_db):
    # The scope's guard casts its parameter to the column's type, as the server spells it (Int32).
    with Tenancy.scope(1):
        document = await TenantDocument.objects.create(id=21, title="a")
        document.title = "b"
        await document.save()
        assert await TenantDocument.objects.filter(id=21).values_list("title", flat=True) == ["b"]
