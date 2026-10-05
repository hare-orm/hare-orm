"""Meta.table_options - each dialect's own storage options, applied on create, rebuild and migration."""

import pytest

from hare.dialects.postgresql.constants import POSTGRESQL_DIALECT
from hare.dialects.postgresql.postgresql_table_options import PostgresqlTableOptions
from hare.dialects.sqlite.constants import SQLITE_DIALECT
from hare.dialects.sqlite.sqlite_table_options import SqliteTableOptions
from hare.exceptions import ConfigurationError
from hare.fields import CharField, IntField
from hare.inspectdb import SchemaInspector
from hare.migrations.drift import detect_drift
from hare.migrations.operations import AlterModelOptions
from hare.migrations.writer import ImportManager, MigrationWriter
from hare.models import Model
from tests.migrations.test_round_trip_real_db import APP_LABEL, RoundTrip, build_live_state, build_model
from tests.utils.fake_client import FakeClient


def test_options_render_their_own_dialect_sql():
    assert SqliteTableOptions(without_rowid=True).get_create_suffix_sql(Model, str) == " WITHOUT ROWID"
    assert SqliteTableOptions().get_create_suffix_sql(Model, str) == ""
    options = PostgresqlTableOptions(
        tablespace="fast", unlogged=True, storage_parameters={"fillfactor": 70, "autovacuum_enabled": False}
    )
    assert options.get_create_prefix_sql() == "UNLOGGED "
    assert options.get_create_suffix_sql(Model, lambda name: f'"{name}"') == (
        ' WITH (autovacuum_enabled = false, fillfactor = 70) TABLESPACE "fast"'
    )


@pytest.mark.parametrize(
    "storage_parameters",
    [{"fill factor": 70}, {"fillfactor; DROP TABLE x": 1}, {"fillfactor": [70]}],
)
def test_postgresql_storage_parameters_are_checked(storage_parameters):
    with pytest.raises(ConfigurationError, match="storage parameter"):
        PostgresqlTableOptions(storage_parameters=storage_parameters)


def test_meta_takes_one_entry_per_dialect():
    with pytest.raises(ConfigurationError, match="two entries for the sqlite dialect"):
        build_model(
            "TwiceOptioned",
            "twice_optioned",
            {},
            {"table_options": [SqliteTableOptions(), SqliteTableOptions(without_rowid=True)]},
        )
    with pytest.raises(ConfigurationError, match="must be TableOptions"):
        build_model("WronglyOptioned", "wrongly_optioned", {}, {"table_options": [{"without_rowid": True}]})


def test_a_connection_uses_its_own_dialect_entry():
    model = build_model(
        "BothOptioned",
        "both_optioned",
        {"id": IntField(primary_key=True, generated=False), "code": CharField(max_length=20)},
        {"table_options": [SqliteTableOptions(without_rowid=True), PostgresqlTableOptions(unlogged=True)]},
    )
    sqlite_sql = (
        SQLITE_DIALECT.schema_editor_class(FakeClient("sqlite")).table_creation.get_model_sql_data(model).table_sql
    )
    postgresql_sql = (
        POSTGRESQL_DIALECT.schema_editor_class(FakeClient("postgresql"))
        .table_creation.get_model_sql_data(model)
        .table_sql
    )
    assert sqlite_sql.startswith('CREATE TABLE "both_optioned"')
    assert ") WITHOUT ROWID;" in sqlite_sql
    assert postgresql_sql.startswith('CREATE UNLOGGED TABLE "both_optioned"')
    assert "WITHOUT ROWID" not in postgresql_sql


def test_options_are_written_into_migrations():
    imports = ImportManager()
    rendered = MigrationWriter.render_value(
        (SqliteTableOptions(without_rowid=True), PostgresqlTableOptions(storage_parameters={"fillfactor": 70})),
        imports,
    )
    assert "SqliteTableOptions(without_rowid=True)" in rendered
    assert "PostgresqlTableOptions(storage_parameters={'fillfactor': 70})" in rendered


def build_gauge(table_options):
    return build_model(
        "OptionedGauge",
        "optioned_gauge",
        {"id": IntField(primary_key=True, generated=False), "code": CharField(max_length=20)},
        {"table_options": table_options},
    )


def test_without_rowid_rejects_a_generated_primary_key():
    model = build_model("RowidGauge", "rowid_gauge", {}, {"table_options": [SqliteTableOptions(without_rowid=True)]})
    editor = SQLITE_DIALECT.schema_editor_class(FakeClient("sqlite"))
    with pytest.raises(ConfigurationError, match="without_rowid=True"):
        editor.table_creation.get_model_sql_data(model)


def test_without_rowid_rejects_a_model_without_a_primary_key():
    model = type(
        "RowidLog",
        (Model,),
        {
            "note": CharField(max_length=20),
            "Meta": type(
                "Meta",
                (),
                {
                    "table": "rowid_log",
                    "app": APP_LABEL,
                    "primary_key": None,
                    "table_options": [SqliteTableOptions(without_rowid=True)],
                },
            ),
        },
    )
    editor = SQLITE_DIALECT.schema_editor_class(FakeClient("sqlite"))
    with pytest.raises(ConfigurationError, match=r"without_rowid=True\) needs a primary key"):
        editor.table_creation.get_model_sql_data(model)


@pytest.mark.asyncio
async def test_sqlite_options_are_created_and_changed_by_migrations(db_isolated):
    round_trip = RoundTrip(db_isolated.get_connection())
    if round_trip.connection.dialect.name != "sqlite":
        pytest.skip("SQLite's own table options")
    await round_trip.migrate_to(build_gauge([]))
    await round_trip.connection.execute_script("INSERT INTO optioned_gauge (id, code) VALUES (1, 'a')")

    operations = await round_trip.migrate_to(build_gauge([SqliteTableOptions(without_rowid=True)]))

    assert [type(operation) for operation in operations] == [AlterModelOptions]
    (row,) = await round_trip.connection.execute_dicts(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'optioned_gauge'"
    )
    assert row["sql"].endswith("WITHOUT ROWID")
    assert await round_trip.connection.execute_dicts("SELECT id, code FROM optioned_gauge") == [{"id": 1, "code": "a"}]
    assert round_trip.get_pending_operations(build_gauge([SqliteTableOptions(without_rowid=True)])) == []


@pytest.mark.asyncio
async def test_postgresql_options_are_created_and_changed_in_place(db_isolated):
    round_trip = RoundTrip(db_isolated.get_connection())
    if round_trip.dialect != "postgresql":
        pytest.skip("PostgreSQL's own table options")

    async def get_storage() -> dict:
        (row,) = await round_trip.connection.execute_dicts(
            "SELECT relpersistence::text AS relpersistence, reloptions FROM pg_class "
            "WHERE oid = 'optioned_gauge'::regclass"
        )
        return {"persistence": row["relpersistence"], "options": sorted(row["reloptions"] or [])}

    await round_trip.migrate_to(
        build_gauge([PostgresqlTableOptions(unlogged=True, storage_parameters={"fillfactor": 70})])
    )
    await round_trip.connection.execute_script("INSERT INTO optioned_gauge (id, code) VALUES (1, 'a')")
    assert await get_storage() == {"persistence": "u", "options": ["fillfactor=70"]}

    await round_trip.migrate_to(
        build_gauge([PostgresqlTableOptions(storage_parameters={"autovacuum_enabled": False})])
    )

    assert await get_storage() == {"persistence": "p", "options": ["autovacuum_enabled=false"]}
    assert await round_trip.connection.execute_dicts("SELECT id, code FROM optioned_gauge") == [{"id": 1, "code": "a"}]


async def get_drift_operations(round_trip: RoundTrip, model: type[Model]) -> list[type]:
    drift = await detect_drift(round_trip.connection, build_live_state(model), [APP_LABEL])
    return [type(operation) for operation in drift.operations]


@pytest.mark.asyncio
async def test_drift_and_inspectdb_read_sqlite_options(db_isolated):
    round_trip = RoundTrip(db_isolated.get_connection())
    if round_trip.connection.dialect.name != "sqlite":
        pytest.skip("SQLite's own table options")
    rowid_less = build_gauge([SqliteTableOptions(without_rowid=True)])
    await round_trip.migrate_to(rowid_less)

    assert await get_drift_operations(round_trip, rowid_less) == []
    assert await get_drift_operations(round_trip, build_gauge([])) == [AlterModelOptions]
    # Another dialect's entry can't be seen in this database and isn't reported.
    assert (
        await get_drift_operations(
            round_trip, build_gauge([SqliteTableOptions(without_rowid=True), PostgresqlTableOptions(unlogged=True)])
        )
        == []
    )
    source = await SchemaInspector.inspect(round_trip.connection, ["optioned_gauge"])
    assert "table_options = [SqliteTableOptions(without_rowid=True)]" in source

    await round_trip.migrate_to(build_gauge([]))
    assert await get_drift_operations(round_trip, build_gauge([])) == []
    assert await get_drift_operations(round_trip, rowid_less) == [AlterModelOptions]
    assert "table_options" not in await SchemaInspector.inspect(round_trip.connection, ["optioned_gauge"])


@pytest.mark.asyncio
async def test_drift_and_inspectdb_read_postgresql_options(db_isolated):
    round_trip = RoundTrip(db_isolated.get_connection())
    if round_trip.dialect != "postgresql":
        pytest.skip("PostgreSQL's own table options")
    stored = build_gauge(
        [PostgresqlTableOptions(unlogged=True, storage_parameters={"fillfactor": 70, "autovacuum_enabled": False})]
    )
    await round_trip.migrate_to(stored)

    assert await get_drift_operations(round_trip, stored) == []
    assert await get_drift_operations(round_trip, build_gauge([])) == [AlterModelOptions]
    assert await get_drift_operations(
        round_trip, build_gauge([PostgresqlTableOptions(unlogged=True, storage_parameters={"fillfactor": 80})])
    ) == [AlterModelOptions]
    source = await SchemaInspector.inspect(round_trip.connection, ["optioned_gauge"])
    assert (
        "table_options = [PostgresqlTableOptions(unlogged=True, storage_parameters="
        "{'autovacuum_enabled': False, 'fillfactor': 70})]"
    ) in source


@pytest.mark.asyncio
async def test_drift_takes_the_default_tablespace_named_explicitly(db_isolated):
    round_trip = RoundTrip(db_isolated.get_connection())
    if round_trip.dialect != "postgresql":
        pytest.skip("PostgreSQL's own table options")
    (row,) = await round_trip.connection.execute_dicts(
        "SELECT ts.spcname FROM pg_database db JOIN pg_tablespace ts ON ts.oid = db.dattablespace "
        "WHERE db.datname = current_database()"
    )
    on_default_tablespace = build_gauge([PostgresqlTableOptions(tablespace=row["spcname"])])
    await round_trip.migrate_to(on_default_tablespace)

    assert await get_drift_operations(round_trip, on_default_tablespace) == []
    assert await get_drift_operations(round_trip, build_gauge([])) == []

    # Taking the tablespace away moves the table to the database's default one.
    await round_trip.migrate_to(build_gauge([]))
    (stored,) = await round_trip.connection.execute_dicts(
        "SELECT reltablespace FROM pg_class WHERE oid = 'optioned_gauge'::regclass"
    )
    assert stored["reltablespace"] == 0
    assert await get_drift_operations(round_trip, build_gauge([])) == []
