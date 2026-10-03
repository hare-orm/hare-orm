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
from hare.contrib.test.helpers import hare_test_context
from hare.dialects.base.db_url import DbUrlConfigGenerator
from hare.dialects.postgresql.client import PostgresqlClient
from hare.dialects.postgresql.constants import REUSABLE_TEST_DATABASE_LEASE_CREDENTIAL
from hare.exceptions import ConfigurationError
from hare.models import Model
from tests.utils.database_under_test import DatabaseUnderTest

OWNED_NAME_PATTERN = re.compile(r"^test_([0-9a-f]{12})_[0-9a-f]{32}$")
EXTENSIONS_TO_INSTALL = ("citext", "vector", "btree_gist", "postgis")

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
        admin_client = ctx.db()
        database_names = await _get_tagged_database_names(admin_client, tag)
    for database_name in database_names:
        dropping_client = type(admin_client)(
            connection_name="pool_template_cleanup",
            user=admin_client.user,
            password=admin_client.password,
            database=database_name,
            host=admin_client.host,
            port=admin_client.port,
        )
        await dropping_client.db_delete()


class TestSwitch:
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

    @pytest.mark.parametrize("raw_value", ["yes", "", "2", "on"])
    def test_rejects_other_environment_values(self, monkeypatch: pytest.MonkeyPatch, raw_value: str) -> None:
        monkeypatch.setenv(REUSE_DATABASES_ENVIRONMENT_VARIABLE, raw_value)
        with pytest.raises(ConfigurationError, match=REUSE_DATABASES_ENVIRONMENT_VARIABLE):
            ReusableTestDatabases.is_enabled()

    def test_explicit_argument_wins_over_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(REUSE_DATABASES_ENVIRONMENT_VARIABLE, "1")
        assert ReusableTestDatabases.is_enabled(False) is False
        monkeypatch.setenv(REUSE_DATABASES_ENVIRONMENT_VARIABLE, "0")
        assert ReusableTestDatabases.is_enabled(True) is True

    def test_rejects_a_non_bool_argument(self) -> None:
        with pytest.raises(ConfigurationError, match="reuse_databases"):
            ReusableTestDatabases.is_enabled(1)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_hare_test_context_validates_the_environment_variable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(REUSE_DATABASES_ENVIRONMENT_VARIABLE, "maybe")
        with pytest.raises(ConfigurationError, match=REUSE_DATABASES_ENVIRONMENT_VARIABLE):
            async with hare_test_context([__name__]):
                pass


class TestSlotNames:
    def test_slot_ids_are_stable_and_distinct(self) -> None:
        slot_ids = [ReusableTestDatabases.get_slot_id(slot_number) for slot_number in range(5)]
        assert slot_ids == [ReusableTestDatabases.get_slot_id(slot_number) for slot_number in range(5)]
        assert len(set(slot_ids)) == 5
        assert all(re.fullmatch(r"[0-9a-f]{32}", slot_id) for slot_id in slot_ids)

    def test_expand_leases_a_slot_named_like_an_owned_test_database(self) -> None:
        tag = uuid.uuid4().hex[:12]
        with ReusableTestDatabases.track_new_leases() as own_lease_number_by_database_name:
            first = DbUrlConfigGenerator.expand(
                f"postgresql://u:p@h/test_{tag}_{{}}", testing=True, reuse_databases=True
            )
            second = DbUrlConfigGenerator.expand(
                f"postgresql://u:p@h/test_{tag}_{{}}", testing=True, reuse_databases=True
            )
        try:
            first_name = first["credentials"]["database"]
            second_name = second["credentials"]["database"]
            assert OWNED_NAME_PATTERN.match(first_name)
            assert OWNED_NAME_PATTERN.match(second_name)
            assert first_name != second_name
            assert first_name == f"test_{tag}_{ReusableTestDatabases.get_slot_id(0)}"
            assert set(own_lease_number_by_database_name) == {first_name, second_name}
            assert (
                first["credentials"][REUSABLE_TEST_DATABASE_LEASE_CREDENTIAL]
                == (own_lease_number_by_database_name[first_name])
            )
        finally:
            ReusableTestDatabases.release_leases(own_lease_number_by_database_name)
        assert not ReusableTestDatabases.is_leased(first_name, own_lease_number_by_database_name[first_name])

    def test_expand_without_reuse_or_placeholder_leases_nothing(self) -> None:
        with ReusableTestDatabases.track_new_leases() as own_lease_number_by_database_name:
            random_name = DbUrlConfigGenerator.expand("postgresql://u:p@h/test_{}", testing=True)
            fixed_name = DbUrlConfigGenerator.expand("postgresql://u:p@h/fixed", testing=True, reuse_databases=True)
            sqlite_file = DbUrlConfigGenerator.expand("sqlite:///db_{}.sqlite3", testing=True, reuse_databases=True)
        assert own_lease_number_by_database_name == {}
        for config in (random_name, fixed_name, sqlite_file):
            assert REUSABLE_TEST_DATABASE_LEASE_CREDENTIAL not in config["credentials"]
        assert fixed_name["credentials"]["database"] == "fixed"

    def test_release_of_a_stale_lease_keeps_the_newer_one(self) -> None:
        template = f"test_{uuid.uuid4().hex[:12]}_{{}}"
        database_name, first_lease_number = ReusableTestDatabases.acquire(template)
        ReusableTestDatabases.release(database_name, first_lease_number, is_clean=True)
        same_database_name, second_lease_number = ReusableTestDatabases.acquire(template)
        try:
            assert same_database_name == database_name
            ReusableTestDatabases.release(database_name, first_lease_number, is_clean=True)
            assert ReusableTestDatabases.is_leased(database_name, second_lease_number)
        finally:
            ReusableTestDatabases.release(database_name, second_lease_number, is_clean=False)
        assert not ReusableTestDatabases.is_clean(database_name)


@requires_postgres
class TestPostgresPool:
    @pytest.mark.asyncio
    async def test_reset_brings_back_a_fresh_database(self, pool_template: tuple[str, str]) -> None:
        template, _ = pool_template
        gid = f"hare_pool_test_{uuid.uuid4().hex}"
        async with hare_test_context([__name__], db_url=template, reuse_databases=True) as ctx:
            client = ctx.db()
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
            client = ctx.db()
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
                    observed_name_pairs.append((outer.db().database, inner.db().database))
                    admin_client = inner.db()
                    assert len(await _get_tagged_database_names(admin_client, tag)) == 2
        assert len(set(observed_name_pairs)) == 1
        outer_name, inner_name = observed_name_pairs[0]
        assert outer_name != inner_name
        async with hare_test_context([__name__], db_url=template, reuse_databases=True) as ctx:
            assert ctx.db().database == outer_name
            assert await _get_tagged_database_names(ctx.db(), tag) == {outer_name, inner_name}

    @pytest.mark.asyncio
    async def test_series_of_contexts_keeps_one_database_per_slot(self, pool_template: tuple[str, str]) -> None:
        template, tag = pool_template
        database_names = set()
        for widget_number in range(5):
            async with hare_test_context([__name__], db_url=template, reuse_databases=True) as ctx:
                await PooledWidget.objects.create(name=f"widget {widget_number}")
                assert await PooledWidget.objects.all().count() == 1
                database_names.add(ctx.db().database)
                assert await _get_tagged_database_names(ctx.db(), tag) == database_names
        assert len(database_names) == 1

    @pytest.mark.asyncio
    async def test_slot_is_released_when_the_body_raises(self, pool_template: tuple[str, str]) -> None:
        template, _ = pool_template
        with pytest.raises(RuntimeError, match="boom"):
            async with hare_test_context([__name__], db_url=template, reuse_databases=True) as ctx:
                failed_database_name = ctx.db().database
                failed_lease_number = ctx.db().reusable_test_database_lease
                await PooledWidget.objects.create(name="before the failure")
                raise RuntimeError("boom")
        assert failed_lease_number is not None
        assert not ReusableTestDatabases.is_leased(failed_database_name, failed_lease_number)
        assert ReusableTestDatabases.is_clean(failed_database_name)
        async with hare_test_context([__name__], db_url=template, reuse_databases=True) as ctx:
            assert ctx.db().database == failed_database_name
            assert await PooledWidget.objects.all().count() == 0

    @pytest.mark.asyncio
    async def test_slot_is_released_when_the_reset_fails_and_reset_before_its_next_use(
        self, pool_template: tuple[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        template, _ = pool_template

        async def failing_reset(client: PostgresqlClient) -> bool:
            raise RuntimeError("reset failed")

        with pytest.raises(RuntimeError, match="reset failed"):
            async with hare_test_context([__name__], db_url=template, reuse_databases=True) as ctx:
                dirty_database_name = ctx.db().database
                await PooledWidget.objects.create(name="left behind")
                monkeypatch.setattr(PostgresqlClient, "_reset_database", failing_reset)
        monkeypatch.undo()
        assert not ReusableTestDatabases.is_clean(dirty_database_name)
        async with hare_test_context([__name__], db_url=template, reuse_databases=True) as ctx:
            assert ctx.db().database == dirty_database_name
            assert await PooledWidget.objects.all().count() == 0

    @pytest.mark.asyncio
    async def test_slot_is_released_when_init_fails(self, pool_template: tuple[str, str]) -> None:
        template, tag = pool_template
        with pytest.raises(ConfigurationError):
            async with hare_test_context([__name__], db_url=template, reuse_databases=True, timezone="Not/AZone"):
                pass
        assert not any(
            database_name.startswith(f"test_{tag}_")
            for database_name in ReusableTestDatabases.lease_number_by_database_name
        )
        async with hare_test_context([__name__], db_url=template, reuse_databases=True) as ctx:
            assert ctx.db().database == f"test_{tag}_{ReusableTestDatabases.get_slot_id(0)}"

    @pytest.mark.asyncio
    async def test_disabled_pool_creates_and_drops_a_random_database(self, pool_template: tuple[str, str]) -> None:
        template, tag = pool_template
        async with hare_test_context([__name__], db_url=template, reuse_databases=False) as ctx:
            random_database_name = ctx.db().database
            assert random_database_name.removeprefix(f"test_{tag}_") not in {
                ReusableTestDatabases.get_slot_id(slot_number) for slot_number in range(8)
            }
        async with hare_test_context([__name__], db_url=template, reuse_databases=True) as ctx:
            assert random_database_name not in await _get_tagged_database_names(ctx.db(), tag)
