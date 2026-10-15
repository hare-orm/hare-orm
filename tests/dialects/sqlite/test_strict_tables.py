"""STRICT tables on SQLite (3.37+): SqliteTableOptions(strict=True) - columns declared with the
STRICT type of their affinity, values of every field kept, migrations rebuilding the table."""

import datetime
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio

from hare import fields
from hare.contrib.test import hare_test_context
from hare.dialects.dialect_registry import DialectRegistry
from hare.dialects.sqlite.drivers.aiosqlite.client import AiosqliteClient
from hare.dialects.sqlite.sqlite_table_options import SqliteTableOptions
from hare.exceptions import ConfigurationError, IntegrityError, UnSupportedError
from hare.inspectdb import SchemaInspector
from hare.migrations.drift import detect_drift
from hare.migrations.operations import AddField, AlterModelOptions, CreateModel
from hare.migrations.state.state import State
from hare.migrations.state.state_apps import StateApps
from tests.dialects.sqlite.models_strict import StrictKeyed, StrictOwner, StrictRecord
from tests.migrations.test_round_trip_real_db import APP_LABEL, RoundTrip, build_live_state, build_model

#: A test creating STRICT tables runs only on a SQLite library that has them.
requires_strict_tables = pytest.mark.skipif(
    not AiosqliteClient.features.supports_strict_tables, reason="the SQLite library has no STRICT tables"
)


@pytest_asyncio.fixture
async def strict_db():
    async with hare_test_context(
        ["tests.dialects.sqlite.models_strict"], db_url="sqlite+aiosqlite://:memory:"
    ) as context:
        yield context


async def get_table_sql(connection, table: str) -> str:
    rows = await connection.execute_dicts("SELECT sql FROM sqlite_master WHERE name = ?", [table])
    return rows[0]["sql"]


@pytest.mark.asyncio
@requires_strict_tables
async def test_columns_get_the_strict_type_of_their_affinity(strict_db):
    table_sql = await get_table_sql(StrictRecord.get_connection(), "strict_record")
    assert table_sql.rstrip().endswith(") STRICT")
    columns = await StrictRecord.get_connection().execute_dicts("PRAGMA table_info(strict_record)")
    assert {column["name"]: column["type"] for column in columns} == {
        "id": "INTEGER",
        "owner_id": "INTEGER",
        "flag": "INTEGER",
        "count": "INTEGER",
        "ratio": "REAL",
        "amount": "TEXT",
        "label": "TEXT",
        "text": "TEXT",
        "day": "ANY",
        "moment": "ANY",
        "clock": "ANY",
        "data": "TEXT",
        "token": "TEXT",
        "payload": "BLOB",
    }
    keyed_sql = await get_table_sql(StrictKeyed.get_connection(), "strict_keyed")
    assert keyed_sql.rstrip().endswith(") STRICT, WITHOUT ROWID")


@pytest.mark.asyncio
@requires_strict_tables
async def test_every_field_value_round_trips(strict_db):
    owner = await StrictOwner.objects.create(name="owner")
    token = uuid.uuid4()
    moment = datetime.datetime(2026, 10, 5, 12, 30, 15, 123456, tzinfo=datetime.UTC)
    record = await StrictRecord.objects.create(
        owner=owner,
        flag=True,
        count=7,
        ratio=0.25,
        amount=Decimal("12.50"),
        label="label",
        text="text",
        day=datetime.date(2026, 10, 5),
        moment=moment,
        clock=datetime.time(8, 15),
        data={"key": [1, 2.5, None]},
        token=token,
        payload=b"\x00\x01",
    )
    loaded = await StrictRecord.objects.select_related("owner").get(id=record.id)
    assert loaded.owner.name == "owner"
    assert (loaded.flag, loaded.count, loaded.ratio, loaded.amount) == (True, 7, 0.25, Decimal("12.50"))
    assert (loaded.label, loaded.text, loaded.day, loaded.moment) == (
        "label",
        "text",
        datetime.date(2026, 10, 5),
        moment,
    )
    assert (loaded.clock.replace(tzinfo=None), loaded.data, loaded.token, loaded.payload) == (
        datetime.time(8, 15),
        {"key": [1, 2.5, None]},
        token,
        b"\x00\x01",
    )
    assert await StrictRecord.objects.filter(amount__gt=Decimal("12.4"), day__year=2026, flag=True).count() == 1
    await StrictRecord.objects.filter(id=record.id).update(count=8, ratio=1)
    assert (await StrictRecord.objects.get(id=record.id)).ratio == 1.0
    await StrictKeyed.objects.create(code="a", hits=1)
    assert await StrictKeyed.objects.filter(code="a").values_list("hits", flat=True) == [1]


@pytest.mark.asyncio
@requires_strict_tables
async def test_sqlite_refuses_a_value_of_another_type(strict_db):
    with pytest.raises(IntegrityError, match="cannot store TEXT value in INTEGER column"):
        await StrictOwner.get_connection().execute(
            "INSERT INTO strict_record (id, count, flag, token) VALUES (1, 'many', 0, 'x')"
        )


@pytest.mark.parametrize(
    ("server_version", "supported"),
    [((3, 36, 0), False), ((3, 37, 0), True), ((3, 45, 1), True)],
)
def test_strict_tables_need_sqlite_3_37(server_version, supported):
    dialect = DialectRegistry.get_dialect("sqlite")
    assert dialect.get_server_version_features(server_version)["supports_strict_tables"] is supported


@pytest.mark.asyncio
async def test_an_older_sqlite_refuses_a_strict_table_before_any_sql():
    client = AiosqliteClient(file_path=":memory:", connection_alias="old_sqlite")
    await client.create_connection(with_db=True)
    client.features = client.features.replace(supports_strict_tables=False)
    editor = client.dialect.schema_editor_class(client, atomic=True, collect_sql=False)
    statements: list[str] = []

    async def record_sql(sql: str) -> None:
        statements.append(sql)

    editor.run_sql = record_sql
    try:
        with pytest.raises(UnSupportedError, match=r"strict=True\) needs SQLite 3\.37\.0"):
            await CreateModel(
                name="Old",
                fields=[("id", fields.IntField(primary_key=True))],
                options={"table": "old_strict", "table_options": [SqliteTableOptions(strict=True)]},
            ).run(APP_LABEL, State(models={}, apps=StateApps()), dry_run=False, state_editor=editor)
    finally:
        await client.close()
    assert statements == []


def test_options_must_be_bools():
    with pytest.raises(ConfigurationError, match="strict must be a bool"):
        SqliteTableOptions(strict="yes")
    with pytest.raises(ConfigurationError, match="without_rowid must be a bool"):
        SqliteTableOptions(without_rowid=1)
    assert SqliteTableOptions(strict=True).deconstruct() == (
        "hare.dialects.sqlite.sqlite_table_options.SqliteTableOptions",
        [],
        {"strict": True},
    )


@pytest_asyncio.fixture
async def round_trip():
    async with hare_test_context(
        ["tests.dialects.sqlite.models_strict"], db_url="sqlite+aiosqlite://:memory:"
    ) as context:
        yield RoundTrip(context.get_connection())


def build_counter(table_options: list[SqliteTableOptions], **extra_fields) -> type:
    return build_model(
        "Counter",
        "strict_counter",
        {"label": fields.CharField(max_length=10), "when": fields.DateField(null=True), **extra_fields},
        {"table_options": table_options},
    )


@pytest.mark.asyncio
@requires_strict_tables
async def test_migrations_make_a_table_strict_and_back(round_trip):
    loose = build_counter([])
    await round_trip.migrate_to(loose)
    await round_trip.connection.execute_script(
        "INSERT INTO strict_counter (id, label, \"when\") VALUES (1, 'kept', '2026-10-05')"
    )
    strict = build_counter([SqliteTableOptions(strict=True)])
    operations = await round_trip.migrate_to(strict)
    assert [type(operation).__name__ for operation in operations] == ["AlterModelOptions"]
    assert (await get_table_sql(round_trip.connection, "strict_counter")).rstrip().endswith("STRICT")
    rows = await round_trip.connection.execute_dicts('SELECT id, label, "when" FROM strict_counter')
    assert [(row["id"], row["label"], row["when"]) for row in rows] == [(1, "kept", "2026-10-05")]
    assert (await detect_drift(round_trip.connection, build_live_state(strict), [APP_LABEL])).operations == []
    source = await SchemaInspector.inspect(round_trip.connection, ["strict_counter"])
    assert "table_options = [SqliteTableOptions(strict=True)]" in source
    widened = build_counter([SqliteTableOptions(strict=True)], note=fields.CharField(max_length=5, null=True))
    await round_trip.migrate_to(widened)
    columns = await round_trip.connection.execute_dicts("PRAGMA table_info(strict_counter)")
    assert {column["name"]: column["type"] for column in columns}["note"] == "TEXT"
    await round_trip.migrate_to(build_counter([], note=fields.CharField(max_length=5, null=True)))
    assert not (await get_table_sql(round_trip.connection, "strict_counter")).rstrip().endswith("STRICT")
    rows = await round_trip.connection.execute_dicts("SELECT label FROM strict_counter")
    assert [row["label"] for row in rows] == ["kept"]


@pytest.mark.asyncio
@requires_strict_tables
async def test_strict_add_field_and_rebuild_on_a_live_table():
    client = AiosqliteClient(file_path=":memory:", connection_alias="strict_add_field")
    await client.create_connection(with_db=True)
    try:
        editor = client.dialect.schema_editor_class(client, atomic=True, collect_sql=False)
        state = State(models={}, apps=StateApps())
        await CreateModel(
            name="Tally",
            fields=[
                ("id", fields.IntField(primary_key=True, generated=False)),
                ("total", fields.DecimalField(max_digits=8, decimal_places=2)),
            ],
            options={"table": "strict_tally", "table_options": [SqliteTableOptions(strict=True)]},
        ).run(APP_LABEL, state, dry_run=False, state_editor=editor)
        await AddField("Tally", "seen", fields.DatetimeField(null=True)).run(
            APP_LABEL, state, dry_run=False, state_editor=editor
        )
        await AlterModelOptions("Tally", {"table_options": [SqliteTableOptions(strict=True, without_rowid=True)]}).run(
            APP_LABEL, state, dry_run=False, state_editor=editor
        )
        columns = await client.execute_dicts("PRAGMA table_info(strict_tally)")
        table_sql = await get_table_sql(client, "strict_tally")
        with pytest.raises(IntegrityError, match="cannot store BLOB value in TEXT column"):
            await client.execute("INSERT INTO strict_tally (id, total) VALUES (1, x'00')")
    finally:
        await client.close()
    assert {column["name"]: column["type"] for column in columns} == {"id": "INTEGER", "total": "TEXT", "seen": "ANY"}
    assert table_sql.rstrip().endswith("STRICT, WITHOUT ROWID")
