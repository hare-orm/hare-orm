"""Tests for ReusableTestDatabases - Postgres test databases reset and reused instead of dropped."""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio

from hare import fields
from hare.contrib.test import ReusableTestDatabases
from hare.contrib.test.constants import REUSE_DATABASES_ENVIRONMENT_VARIABLE
from hare.contrib.test.databases.declarations import TestDatabaseIdentity as DatabaseIdentity
from hare.contrib.test.databases.kept_test_schema import KeptTestSchema
from hare.contrib.test.databases.postgresql.reusable_test_schemas import ReusableTestSchemas
from hare.contrib.test.databases.postgresql.test_database import PostgresqlTestDatabase
from hare.contrib.test.databases.test_database_config import TestDatabaseConfig as DatabaseConfig
from hare.contrib.test.isolated_contexts import hare_test_context
from hare.dialects.base.connection.db_url_config_generator import DbUrlConfigGenerator
from hare.dialects.postgresql.client import PostgresqlClient
from hare.exceptions import ConfigurationError
from hare.models import Model
from tests.connections.reusable_schema_models import KeptCounter
from tests.utils.database_under_test import DatabaseUnderTest
from tests.utils.postgresql_extensions import PostgresqlExtensions

OWNED_NAME_PATTERN = re.compile(r"^test_([0-9a-f]{12})_[0-9a-f]{32}$")
EXTENSIONS_TO_INSTALL = ("citext", "vector", "btree_gist", "postgis")
KEPT_SCHEMA_MODELS = "tests.connections.reusable_schema_models"

requires_postgres = pytest.mark.skipif(
    DatabaseUnderTest.is_file_database(),
    reason="reusable test databases only apply to Postgres",
)


class PooledWidget(Model):
    id = fields.IntField(primary_key=True)
    name = fields.TextField()


def _get_postgres_template(tag: str) -> str:
    raw_db_url = os.environ["HARE_TEST_DB"].replace("\\{", "{").replace("\\}", "}")
    base_url, _, database_and_query = raw_db_url.rpartition("/")
    _, question_mark, query = database_and_query.partition("?")
    return f"{base_url}/test_{tag}_{{}}{question_mark}{query}"


def _get_database_identity(client: PostgresqlClient) -> DatabaseIdentity:
    host = client.direct_host or client.host or os.environ.get("PGHOST", "")
    port = client.direct_port or client.port
    return DatabaseIdentity((host, int(port)), client.database)


async def _get_public_tables(client: PostgresqlClient) -> list[str]:
    rows = await client.execute_dicts(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'spatial_ref_sys' "
        "ORDER BY tablename"
    )
    return [row["tablename"] for row in rows]


async def _get_table_oid(client: PostgresqlClient, table_name: str) -> int:
    rows = await client.execute_dicts("SELECT $1::regclass::oid AS table_oid", [table_name])
    return int(rows[0]["table_oid"])


async def _get_tagged_database_names(client: PostgresqlClient, tag: str) -> set[str]:
    rows = await client.execute_dicts("SELECT datname FROM pg_database WHERE datname LIKE $1", [f"test\\_{tag}\\_%"])
    return {row["datname"] for row in rows}


@pytest_asyncio.fixture
async def pool_template() -> AsyncGenerator[tuple[str, str]]:
    """A Postgres URL template whose slot databases no other test shares - dropped afterwards."""
    tag = uuid.uuid4().hex[:12]
    template = _get_postgres_template(tag)
    yield template, tag
    async with hare_test_context([__name__], db_url=template, reuse_databases=True) as ctx:
        admin_client = ctx.get_connection()
        database_names = await _get_tagged_database_names(admin_client, tag)
    for database_name in database_names:
        dropping_client = type(admin_client)(
            connection_alias="pool_template_cleanup",
            user=admin_client.user,
            password=admin_client.password,
            database=database_name,
            host=admin_client.host,
            port=admin_client.port,
            transaction_pooling=admin_client.transaction_pooling,
        )
        await dropping_client.db_delete()


class TestSwitch:
    @pytest.mark.database_independent
    def test_disabled_when_the_environment_variable_is_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(REUSE_DATABASES_ENVIRONMENT_VARIABLE, raising=False)
        assert ReusableTestDatabases.is_enabled() is False

    @pytest.mark.parametrize(
        ("raw_value", "expected"), [("1", True), ("TRUE", True), (" 0 ", False), ("false", False)]
    )
    def test_reads_the_environment_variable(
        self, monkeypatch: pytest.MonkeyPatch, raw_value: str, expected: bool
    ) -> None:
        monkeypatch.setenv(REUSE_DATABASES_ENVIRONMENT_VARIABLE, raw_value)
        assert ReusableTestDatabases.is_enabled() is expected

    @pytest.mark.database_independent
    @pytest.mark.parametrize("raw_value", ["yes", "", "2", "on"])
    def test_rejects_other_environment_values(self, monkeypatch: pytest.MonkeyPatch, raw_value: str) -> None:
        monkeypatch.setenv(REUSE_DATABASES_ENVIRONMENT_VARIABLE, raw_value)
        with pytest.raises(ConfigurationError, match=REUSE_DATABASES_ENVIRONMENT_VARIABLE):
            ReusableTestDatabases.is_enabled()

    @pytest.mark.database_independent
    def test_explicit_argument_wins_over_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(REUSE_DATABASES_ENVIRONMENT_VARIABLE, "1")
        assert ReusableTestDatabases.is_enabled(False) is False
        monkeypatch.setenv(REUSE_DATABASES_ENVIRONMENT_VARIABLE, "0")
        assert ReusableTestDatabases.is_enabled(True) is True

    @pytest.mark.database_independent
    @pytest.mark.asyncio
    async def test_hare_test_context_validates_the_environment_variable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(REUSE_DATABASES_ENVIRONMENT_VARIABLE, "maybe")
        with pytest.raises(ConfigurationError, match=REUSE_DATABASES_ENVIRONMENT_VARIABLE):
            async with hare_test_context([__name__]):
                pass


@pytest.mark.database_independent
class TestSlotNames:
    def test_slot_ids_are_stable_and_distinct(self) -> None:
        slot_ids = [ReusableTestDatabases.get_slot_id(slot_number) for slot_number in range(5)]
        assert slot_ids == [ReusableTestDatabases.get_slot_id(slot_number) for slot_number in range(5)]
        assert len(set(slot_ids)) == 5
        assert all(re.fullmatch(r"[0-9a-f]{32}", slot_id) for slot_id in slot_ids)

    def test_test_config_returns_exact_leases_without_credential_tokens(self) -> None:
        tag = uuid.uuid4().hex[:12]
        first = DatabaseConfig.from_url(f"postgresql://u:p@h/test_{tag}_{{}}", reuse=True)
        second = DatabaseConfig.from_url(f"postgresql://u:p@h/test_{tag}_{{}}", reuse=True)
        try:
            assert first.lease is not None and second.lease is not None
            first_name = first.config["credentials"]["database"]
            second_name = second.config["credentials"]["database"]
            assert OWNED_NAME_PATTERN.match(first_name)
            assert OWNED_NAME_PATTERN.match(second_name)
            assert first_name != second_name
            assert first_name == f"test_{tag}_{ReusableTestDatabases.get_slot_id(0)}"
            assert first.lease.identity.database == first_name
            assert set(first.config["credentials"]) == {"host", "port", "user", "password", "database"}
        finally:
            ReusableTestDatabases.release_leases([first.lease, second.lease])
        assert not ReusableTestDatabases.is_leased(first.lease)

    def test_parser_and_nonreusable_configs_acquire_no_leases(self) -> None:
        before = dict(ReusableTestDatabases.leases)
        parsed = DbUrlConfigGenerator.expand("postgresql://u:p@h/test_{}")
        random_name = DatabaseConfig.from_url("postgresql://u:p@h/test_{}")
        fixed_name = DatabaseConfig.from_url("postgresql://u:p@h/fixed", reuse=True)
        sqlite_file = DatabaseConfig.from_url("sqlite+aiosqlite:///db_{}.sqlite3", reuse=True)
        assert parsed["credentials"]["database"] == "test_{}"
        assert ReusableTestDatabases.leases == before
        assert all(config.lease is None for config in (random_name, fixed_name, sqlite_file))
        assert fixed_name.config["credentials"]["database"] == "fixed"

    def test_a_kept_schema_is_read_back_from_its_text(self) -> None:
        kept_schema = KeptTestSchema("schema key", 42, (("pg_class", 10), ("pg_type", 20)), "fingerprint")
        assert KeptTestSchema.from_text(kept_schema.to_text()) == kept_schema

    @pytest.mark.parametrize("text", ["", "not json", "[]", '["key", "not a number", [], "fingerprint"]', "[1, 2, 3]"])
    def test_a_text_holding_no_kept_schema_gives_none(self, text: str) -> None:
        assert KeptTestSchema.from_text(text) is None

    def test_release_of_a_stale_lease_keeps_the_newer_one(self) -> None:
        template = DatabaseIdentity(("server", 5432), f"test_{uuid.uuid4().hex[:12]}_{{}}")
        first = ReusableTestDatabases.acquire(template)
        ReusableTestDatabases.release(first, is_clean=True)
        second = ReusableTestDatabases.acquire(template)
        try:
            assert second.identity == first.identity
            ReusableTestDatabases.release(first, is_clean=True)
            assert ReusableTestDatabases.is_leased(second)
        finally:
            ReusableTestDatabases.release(second, is_clean=False)
        assert not ReusableTestDatabases.is_clean(second.identity)


@requires_postgres
class TestPostgresPool:
    @pytest.mark.asyncio
    async def test_shared_database_reset_clears_qualified_tables_and_sequences(
        self, pool_template: tuple[str, str]
    ) -> None:
        template, _ = pool_template
        async with hare_test_context([__name__], db_url=template, reuse_databases=True) as ctx:
            client = ctx.get_connection()
            await PooledWidget.objects.create(name="before reset")
            await client.execute_script(
                'CREATE SCHEMA "extra schema";'
                'CREATE TABLE "extra schema"."quoted ""table" '
                "(id integer GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, value text);"
                'INSERT INTO "extra schema"."quoted ""table" (value) VALUES (\'before reset\');'
                'CREATE SEQUENCE "extra schema"."independent sequence";'
                'SELECT nextval(\'"extra schema"."independent sequence"\');'
            )
            await ReusableTestSchemas.reset_rows(client)
            assert await PooledWidget.objects.count() == 0
            assert await client.execute_dicts('SELECT * FROM "extra schema"."quoted ""table"') == []
            assert await client.execute_dicts(
                'INSERT INTO "extra schema"."quoted ""table" (value) VALUES (\'after reset\') RETURNING id'
            ) == [{"id": 1}]
            assert await client.execute_dicts(
                'SELECT nextval(\'"extra schema"."independent sequence"\') AS value'
            ) == [{"value": 1}]
            assert (await PooledWidget.objects.create(name="after reset")).id == 1

    @pytest.mark.asyncio
    async def test_reset_brings_back_a_fresh_database(self, pool_template: tuple[str, str]) -> None:
        template, _ = pool_template
        gid = f"hare_pool_test_{uuid.uuid4().hex}"
        async with hare_test_context([__name__], db_url=template, reuse_databases=True) as ctx:
            client = ctx.get_connection()
            database_name = client.database
            available_rows = await client.execute_dicts(
                "SELECT name FROM pg_available_extensions WHERE name = ANY($1::text[])", [list(EXTENSIONS_TO_INSTALL)]
            )
            installed_extensions = sorted(row["name"] for row in available_rows)
            for extension in installed_extensions:
                await client.execute_script(f"CREATE EXTENSION IF NOT EXISTS {extension}")
            await client.execute_script(
                "CREATE SCHEMA leftover_schema; CREATE TABLE leftover_schema.leftover (id int); "
                "CREATE TABLE public.leftover (id int); "
                f"ALTER DATABASE \"{database_name}\" SET work_mem = '8MB'"
            )
            await client.execute_script(
                f"BEGIN; CREATE TABLE public.prepared_leftover (id int); PREPARE TRANSACTION '{gid}'"
            )
            await PooledWidget.objects.create(name="leftover")
            prepared_count = await client.execute_dicts(
                "SELECT count(*) AS prepared_count FROM pg_prepared_xacts WHERE gid = $1", [gid]
            )
            assert prepared_count[0]["prepared_count"] == 1

        async with hare_test_context([__name__], db_url=template, reuse_databases=True) as ctx:
            client = ctx.get_connection()
            assert client.database == database_name
            table_rows = await client.execute_dicts(
                "SELECT table_schema || '.' || table_name AS table_name FROM information_schema.tables "
                "WHERE table_schema NOT IN ('pg_catalog', 'information_schema')"
            )
            assert [row["table_name"] for row in table_rows] == ["public.pooledwidget"]
            assert await PooledWidget.objects.all().count() == 0
            schema_rows = await client.execute_dicts(
                "SELECT nspname, nspowner::regrole::text AS owner, nspacl::text AS acl, "
                "obj_description(oid, 'pg_namespace') AS schema_comment FROM pg_namespace "
                "WHERE nspname NOT LIKE 'pg\\_%' AND nspname <> 'information_schema'"
            )
            assert [row["nspname"] for row in schema_rows] == ["public"]
            assert schema_rows[0]["schema_comment"] == "standard public schema"
            version_rows = await client.execute_dicts(
                "SELECT current_setting('server_version_num')::int AS server_version_number"
            )
            if version_rows[0]["server_version_number"] >= 150000:
                assert schema_rows[0]["owner"] == "pg_database_owner"
                assert schema_rows[0]["acl"] == "{pg_database_owner=UC/pg_database_owner,=U/pg_database_owner}"
            extension_rows = await client.execute_dicts("SELECT extname FROM pg_extension")
            assert [row["extname"] for row in extension_rows] == ["plpgsql"]
            prepared_rows = await client.execute_dicts(
                "SELECT gid FROM pg_prepared_xacts WHERE database = current_database()"
            )
            assert prepared_rows == []
            setting_rows = await client.execute_dicts(
                "SELECT setconfig FROM pg_db_role_setting WHERE setdatabase = "
                "(SELECT oid FROM pg_database WHERE datname = current_database())"
            )
            assert setting_rows == []
            for extension in installed_extensions:
                await client.execute_script(f"CREATE EXTENSION IF NOT EXISTS {extension}")
            if "postgis" in installed_extensions:
                spatial_reference_rows = await client.execute_dicts(
                    "SELECT count(*) AS spatial_reference_count FROM spatial_ref_sys"
                )
                assert spatial_reference_rows[0]["spatial_reference_count"] > 0

    @pytest.mark.asyncio
    async def test_nested_contexts_get_different_databases(self, pool_template: tuple[str, str]) -> None:
        template, tag = pool_template
        observed_name_pairs = []
        for _ in range(3):
            async with hare_test_context([__name__], db_url=template, reuse_databases=True) as outer:
                async with hare_test_context([__name__], db_url=template, reuse_databases=True) as inner:
                    await PooledWidget.objects.create(name="inner")
                    observed_name_pairs.append((outer.get_connection().database, inner.get_connection().database))
                    admin_client = inner.get_connection()
                    assert len(await _get_tagged_database_names(admin_client, tag)) == 2
        assert len(set(observed_name_pairs)) == 1
        outer_name, inner_name = observed_name_pairs[0]
        assert outer_name != inner_name
        async with hare_test_context([__name__], db_url=template, reuse_databases=True) as ctx:
            assert ctx.get_connection().database == outer_name
            assert await _get_tagged_database_names(ctx.get_connection(), tag) == {outer_name, inner_name}

    @pytest.mark.asyncio
    async def test_series_of_contexts_keeps_one_database_per_slot(self, pool_template: tuple[str, str]) -> None:
        template, tag = pool_template
        database_names = set()
        for widget_number in range(5):
            async with hare_test_context([__name__], db_url=template, reuse_databases=True) as ctx:
                await PooledWidget.objects.create(name=f"widget {widget_number}")
                assert await PooledWidget.objects.all().count() == 1
                database_names.add(ctx.get_connection().database)
                assert await _get_tagged_database_names(ctx.get_connection(), tag) == database_names
        assert len(database_names) == 1

    @pytest.mark.asyncio
    async def test_postgis_works_again_in_a_reset_database(self, pool_template: tuple[str, str]) -> None:
        """Past a transaction pooler the reset ran on a pooled session, which kept the dropped PostGIS
        objects cached - a spatial query of the next use failed ("no spatial operator found")."""
        template, _ = pool_template
        for _ in range(3):
            async with hare_test_context([__name__], db_url=template, reuse_databases=True) as ctx:
                client = ctx.get_connection()
                await PostgresqlExtensions.require(client, "postgis")
                await client.execute_script("CREATE EXTENSION IF NOT EXISTS postgis")
                await client.execute_script(
                    "CREATE TABLE spot (id int, location geography(Point, 4326));"
                    "CREATE INDEX spot_location ON spot USING gist (location);"
                    "INSERT INTO spot SELECT n, ST_GeogFromText('POINT(0 0)') FROM generate_series(1, 500) n;"
                    "ANALYZE spot"
                )
                rows = await client.execute_dicts(
                    "SELECT count(*) AS near FROM spot WHERE ST_DWithin(location, ST_GeogFromText('POINT(0 0)'), 10)"
                )
                assert rows == [{"near": 500}]

    @pytest.mark.asyncio
    async def test_slot_is_released_when_the_body_raises(self, pool_template: tuple[str, str]) -> None:
        template, _ = pool_template
        with pytest.raises(RuntimeError, match="boom"):
            async with hare_test_context([__name__], db_url=template, reuse_databases=True) as ctx:
                failed_database_name = ctx.get_connection().database
                failed_identity = _get_database_identity(ctx.get_connection())
                failed_lease = ReusableTestDatabases.leases[failed_identity]
                await PooledWidget.objects.create(name="before the failure")
                raise RuntimeError("boom")
        assert not ReusableTestDatabases.is_leased(failed_lease)
        assert failed_identity in ReusableTestDatabases.kept_schemas
        async with hare_test_context([__name__], db_url=template, reuse_databases=True) as ctx:
            assert ctx.get_connection().database == failed_database_name
            assert await PooledWidget.objects.all().count() == 0

    @pytest.mark.asyncio
    async def test_slot_is_released_when_the_reset_fails_and_reset_before_its_next_use(
        self, pool_template: tuple[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        template, _ = pool_template

        async def failing_reset(resource: PostgresqlTestDatabase) -> bool:
            raise RuntimeError("reset failed")

        with pytest.raises(RuntimeError, match="reset failed"):
            async with hare_test_context([__name__], db_url=template, reuse_databases=True) as ctx:
                dirty_database_name = ctx.get_connection().database
                dirty_identity = _get_database_identity(ctx.get_connection())
                await PooledWidget.objects.create(name="left behind")
                # A changed schema needs the reset.
                await ctx.get_connection().execute_script("CREATE TABLE public.leftover (id int)")
                monkeypatch.setattr(PostgresqlTestDatabase, "_reset_database", failing_reset)
        monkeypatch.undo()
        assert not ReusableTestDatabases.is_clean(dirty_identity)
        assert dirty_identity not in ReusableTestDatabases.kept_schemas
        async with hare_test_context([__name__], db_url=template, reuse_databases=True) as ctx:
            assert ctx.get_connection().database == dirty_database_name
            assert await PooledWidget.objects.all().count() == 0
            assert await _get_public_tables(ctx.get_connection()) == ["pooledwidget"]

    @pytest.mark.asyncio
    async def test_slot_is_reset_before_its_next_use_when_clearing_its_rows_fails(
        self, pool_template: tuple[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        template, _ = pool_template

        async def failing_clear_rows(client: PostgresqlClient, kept_schema: KeptTestSchema) -> KeptTestSchema | None:
            raise RuntimeError("clearing failed")

        with pytest.raises(RuntimeError, match="clearing failed"):
            async with hare_test_context([__name__], db_url=template, reuse_databases=True) as ctx:
                dirty_database_name = ctx.get_connection().database
                dirty_identity = _get_database_identity(ctx.get_connection())
                await PooledWidget.objects.create(name="left behind")
                monkeypatch.setattr(ReusableTestSchemas, "clear_rows", failing_clear_rows)
        monkeypatch.undo()
        assert not ReusableTestDatabases.is_clean(dirty_identity)
        assert dirty_identity not in ReusableTestDatabases.kept_schemas
        async with hare_test_context([__name__], db_url=template, reuse_databases=True) as ctx:
            assert ctx.get_connection().database == dirty_database_name
            assert await PooledWidget.objects.all().count() == 0

    @pytest.mark.asyncio
    async def test_an_unchanged_schema_is_kept_with_its_rows_cleared(self, pool_template: tuple[str, str]) -> None:
        template, _ = pool_template
        async with hare_test_context([KEPT_SCHEMA_MODELS], db_url=template, reuse_databases=True) as ctx:
            database_name = ctx.get_connection().database
            database_identity = _get_database_identity(ctx.get_connection())
            table_oid = await _get_table_oid(ctx.get_connection(), "keptcounter")
            await KeptCounter.objects.create(name="first")
            await KeptCounter.objects.create(name="second")
        assert database_identity in ReusableTestDatabases.kept_schemas
        async with hare_test_context([KEPT_SCHEMA_MODELS], db_url=template, reuse_databases=True) as ctx:
            assert ctx.get_connection().database == database_name
            # The table is the one created before - not created again.
            assert await _get_table_oid(ctx.get_connection(), "keptcounter") == table_oid
            assert await KeptCounter.objects.all().count() == 0
            assert (await KeptCounter.objects.create(name="numbered from the start")).id == 1

    @pytest.mark.asyncio
    async def test_a_schema_whose_tables_were_truncated_is_kept(self, pool_template: tuple[str, str]) -> None:
        """TRUNCATE gives the table new storage - a write of its catalog row, no change of the schema."""
        template, _ = pool_template
        async with hare_test_context([KEPT_SCHEMA_MODELS], db_url=template, reuse_databases=True) as ctx:
            database_identity = _get_database_identity(ctx.get_connection())
            await KeptCounter.objects.create(name="first")
            await ctx.get_connection().execute_script("TRUNCATE keptcounter RESTART IDENTITY")
        assert database_identity in ReusableTestDatabases.kept_schemas

    @pytest.mark.asyncio
    async def test_a_kept_schema_is_read_back_from_the_database_comment(self, pool_template: tuple[str, str]) -> None:
        template, _ = pool_template
        async with hare_test_context([KEPT_SCHEMA_MODELS], db_url=template, reuse_databases=True) as ctx:
            client = ctx.get_connection()
            assert await ReusableTestSchemas.read_kept_schema(client) is None
            kept_schema = await ReusableTestSchemas.get_kept_schema(client, "schema key")
            await ReusableTestSchemas.write_kept_schema(client, kept_schema)
            assert await ReusableTestSchemas.read_kept_schema(client) == kept_schema
            assert not await ReusableTestSchemas.has_schema_changed(client, kept_schema)
            await client.execute_script(f"COMMENT ON DATABASE {client.database} IS 'a comment of a user'")
            assert await ReusableTestSchemas.read_kept_schema(client) is None

    @pytest.mark.asyncio
    async def test_a_changed_schema_is_reset(self, pool_template: tuple[str, str]) -> None:
        template, _ = pool_template
        async with hare_test_context([KEPT_SCHEMA_MODELS], db_url=template, reuse_databases=True) as ctx:
            database_name = ctx.get_connection().database
            database_identity = _get_database_identity(ctx.get_connection())
            await ctx.get_connection().execute_script("ALTER TABLE keptcounter ADD COLUMN extra int")
        assert database_identity not in ReusableTestDatabases.kept_schemas
        async with hare_test_context([KEPT_SCHEMA_MODELS], db_url=template, reuse_databases=True) as ctx:
            assert ctx.get_connection().database == database_name
            column_rows = await ctx.get_connection().execute_dicts(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'keptcounter' "
                "ORDER BY ordinal_position"
            )
            assert [row["column_name"] for row in column_rows] == ["id", "name"]

    @pytest.mark.asyncio
    async def test_a_schema_with_a_dropped_object_is_reset(self, pool_template: tuple[str, str]) -> None:
        """Dropping an object writes no catalog row - only removes some."""
        template, _ = pool_template
        primary_key_sql = (
            "SELECT conname FROM pg_constraint WHERE conrelid = 'keptcounter'::regclass AND contype = 'p'"
        )
        async with hare_test_context([KEPT_SCHEMA_MODELS], db_url=template, reuse_databases=True) as ctx:
            database_name = ctx.get_connection().database
            database_identity = _get_database_identity(ctx.get_connection())
            (primary_key_row,) = await ctx.get_connection().execute_dicts(primary_key_sql)
            primary_key_name = primary_key_row["conname"]
            await ctx.get_connection().execute_script(f'ALTER TABLE keptcounter DROP CONSTRAINT "{primary_key_name}"')
        assert database_identity not in ReusableTestDatabases.kept_schemas
        async with hare_test_context([KEPT_SCHEMA_MODELS], db_url=template, reuse_databases=True) as ctx:
            assert ctx.get_connection().database == database_name
            constraint_rows = await ctx.get_connection().execute_dicts(primary_key_sql)
            assert [row["conname"] for row in constraint_rows] == [primary_key_name]

    @pytest.mark.asyncio
    async def test_a_context_asking_for_another_schema_gets_only_it(self, pool_template: tuple[str, str]) -> None:
        template, _ = pool_template
        async with hare_test_context([KEPT_SCHEMA_MODELS], db_url=template, reuse_databases=True) as ctx:
            database_name = ctx.get_connection().database
        async with hare_test_context([__name__], db_url=template, reuse_databases=True) as ctx:
            assert ctx.get_connection().database == database_name
            assert await _get_public_tables(ctx.get_connection()) == ["pooledwidget"]

    @pytest.mark.asyncio
    async def test_a_context_creating_no_schema_gets_an_empty_database(self, pool_template: tuple[str, str]) -> None:
        template, _ = pool_template
        async with hare_test_context([KEPT_SCHEMA_MODELS], db_url=template, reuse_databases=True) as ctx:
            database_name = ctx.get_connection().database
        async with hare_test_context(
            [KEPT_SCHEMA_MODELS], db_url=template, reuse_databases=True, _generate_schemas=False
        ) as ctx:
            assert ctx.get_connection().database == database_name
            assert await _get_public_tables(ctx.get_connection()) == []

    @pytest.mark.asyncio
    async def test_slot_is_released_when_init_fails(self, pool_template: tuple[str, str]) -> None:
        template, tag = pool_template
        with pytest.raises(ConfigurationError):
            async with hare_test_context([__name__], db_url=template, reuse_databases=True, timezone="Not/AZone"):
                pass
        assert not any(identity.database.startswith(f"test_{tag}_") for identity in ReusableTestDatabases.leases)
        async with hare_test_context([__name__], db_url=template, reuse_databases=True) as ctx:
            assert ctx.get_connection().database == f"test_{tag}_{ReusableTestDatabases.get_slot_id(0)}"

    @pytest.mark.asyncio
    async def test_disabled_pool_creates_and_drops_a_random_database(self, pool_template: tuple[str, str]) -> None:
        template, tag = pool_template
        async with hare_test_context([__name__], db_url=template, reuse_databases=False) as ctx:
            random_database_name = ctx.get_connection().database
            assert random_database_name.removeprefix(f"test_{tag}_") not in {
                ReusableTestDatabases.get_slot_id(slot_number) for slot_number in range(8)
            }
        async with hare_test_context([__name__], db_url=template, reuse_databases=True) as ctx:
            assert random_database_name not in await _get_tagged_database_names(ctx.get_connection(), tag)
