"""How ClickHouse writes rows: the option of lightweight ``UPDATE`` statements checked; a connection with
``async_insert`` has the server gather the inserted rows; the rows a write returns read by their keys."""

import os

import pytest

from hare.dialects.base.connection.db_url_config_generator import DbUrlConfigGenerator
from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
from hare.dialects.clickhouse.constants import CLICKHOUSE_DIALECT
from hare.exceptions import ConfigurationError, UnSupportedError
from tests.dialects.clickhouse.models import Account, Team


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
