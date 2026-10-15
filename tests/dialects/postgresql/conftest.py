"""
Custom fixtures for PostgreSQL-specific tests that require specific model modules.

These fixtures support tests that define hare_test_modules to use
custom model definitions for PostgreSQL features like TSVector.
"""

import os

import pytest
import pytest_asyncio

from hare.contrib.test import RollbackIsolation
from hare.contrib.test.isolated_contexts import hare_test_context
from hare.core.hare_context import HareContext


async def _reset_shared_db_for_module(ctx: HareContext) -> None:
    """Wipes every table in one batched TRUNCATE ... RESTART IDENTITY CASCADE, resetting every
    sequence back to 1 - same helper as the root conftest.py's own db_module uses (duplicated
    rather than imported: pytest's default "prepend" import mode can resolve a bare `import
    conftest` to whichever same-named conftest.py module happens to load first across the whole
    tree, not reliably this project's root one - confirmed live, a full-suite collection raised
    ImportError under that import)."""
    if not ctx.apps:
        return
    tables = list(dict.fromkeys(model._meta.db_table for model in ctx.apps.get_models_iterable()))
    if not tables:
        return
    quote_char = next(iter(ctx.apps.get_models_iterable()))._meta.connection.query_class.SQL_CONTEXT.quote_char
    quoted_tables = ", ".join(f"{quote_char}{table}{quote_char}" for table in tables)
    await ctx.get_connection().execute_script(f"TRUNCATE TABLE {quoted_tables} RESTART IDENTITY CASCADE")  # nosec


def skip_if_not_postgres():
    """Skip test if not running against PostgreSQL."""
    db_url = os.getenv("HARE_TEST_DB", "")
    if db_url.split("://", 1)[0].split("+", 1)[0] != "postgresql":
        pytest.skip("Postgres-only test.")


@pytest_asyncio.fixture(scope="module")
async def db_module_postgres(_shared_test_db_url: str):
    """
    Module-scoped fixture for postgres tests using standard testmodels.

    Reuses the same session-scoped, already-created-and-schema'd database as the root
    conftest.py's own db_module - this fixture used to run its own independent
    hare_test_context() with a full CREATE DATABASE + schema generation per module, missed when
    that per-worker sharing was introduced for db_module itself (this file predates it and lives
    outside its original scope). _reset_shared_db_for_module() gives the same "this file starts
    completely clean" guarantee a fresh-database-per-file used to give, at a fraction of the cost
    - see db_module's own docstring in the root conftest.py for the full rationale.
    """
    skip_if_not_postgres()
    async with hare_test_context(
        modules=["tests.testmodels"],
        db_url=_shared_test_db_url,
        app_label="models",
        connection_label="models",
        _create_db=False,
        _generate_schemas=False,
        _drop_db_on_exit=False,
    ) as ctx:
        await _reset_shared_db_for_module(ctx)
        yield ctx


@pytest_asyncio.fixture(scope="function")
async def db_postgres(db_module_postgres):
    """
    Function-scoped fixture with transaction rollback for postgres tests.

    Equivalent to: test.TestCase with standard testmodels.
    """
    async with RollbackIsolation(db_module_postgres) as ctx:
        yield ctx


@pytest_asyncio.fixture(scope="module")
async def db_tsvector_module():
    """Module-scoped fixture for TestTSVectorField/TestPostgresSearchLookupTSVector - both share
    the same tests.dialects.postgresql.models_tsvector module, so both db_tsvector and db_search
    build on this one create-once-per-file database instead of each recreating it per test."""
    skip_if_not_postgres()
    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    async with hare_test_context(
        modules=["tests.dialects.postgresql.models_tsvector"],
        db_url=db_url,
        app_label="models",
        connection_label="models",
    ) as ctx:
        yield ctx


@pytest_asyncio.fixture(scope="function")
async def db_tsvector(db_tsvector_module):
    """
    Fixture for TestTSVectorField, with transaction rollback cleanup between tests.

    Uses models defined in tests.dialects.postgresql.models_tsvector module.
    Equivalent to: test.IsolatedTestCase with hare_test_modules
    """
    async with RollbackIsolation(db_tsvector_module) as ctx:
        yield ctx


@pytest_asyncio.fixture(scope="function")
async def db_search(db_tsvector_module):
    """
    Fixture for TestPostgresSearchLookupTSVector, with transaction rollback cleanup between
    tests - shares db_tsvector_module's already-created database/schema (same underlying
    tests.dialects.postgresql.models_tsvector module as db_tsvector).

    Equivalent to: test.IsolatedTestCase with hare_test_modules
    """
    async with RollbackIsolation(db_tsvector_module) as ctx:
        yield ctx


@pytest_asyncio.fixture(scope="module")
async def db_postgis_module():
    """
    Module-scoped fixture for PostGISField/STDistance/STDWithin tests - creates the database,
    schema and postgis extension ONCE per file instead of once per test.

    Uses models defined in tests.dialects.postgresql.models_postgis module. Same sequence as
    hare_test_context(), with one extra step (CREATE EXTENSION IF NOT EXISTS postgis) run
    between database creation and schema generation - PostGISField's column type doesn't exist
    until that runs. Skips (not fails) if the extension isn't installable, since most Postgres
    test setups don't have it (needs the postgis/postgis image, not plain postgres).
    """
    skip_if_not_postgres()
    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    async with hare_test_context(
        modules=["tests.dialects.postgresql.models_postgis"],
        db_url=db_url,
        app_label="models",
        connection_label="models",
        _generate_schemas=False,
    ) as ctx:
        try:
            await ctx.get_connection().execute_script("CREATE EXTENSION IF NOT EXISTS postgis;")
        except Exception as exc:
            pytest.skip(f"postgis extension not available: {exc}")
        await ctx.generate_schemas(safe=False)
        yield ctx


@pytest_asyncio.fixture(scope="module")
async def db_gis_module():
    """Module-scoped database of the GeometryField models - generate_schemas() creates the postgis
    extension; skips when the server can't install it."""
    skip_if_not_postgres()
    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    async with hare_test_context(
        modules=["tests.dialects.postgresql.models_gis"],
        db_url=db_url,
        app_label="models",
        connection_label="models",
        _generate_schemas=False,
    ) as ctx:
        try:
            await ctx.get_connection().execute_script("CREATE EXTENSION IF NOT EXISTS postgis;")
        except Exception as exc:
            pytest.skip(f"postgis extension not available: {exc}")
        await ctx.generate_schemas(safe=False)
        yield ctx


@pytest_asyncio.fixture(scope="function")
async def db_gis(db_gis_module):
    """Fixture for the GeometryField tests, with transaction rollback cleanup between tests."""
    async with RollbackIsolation(db_gis_module) as ctx:
        yield ctx


@pytest_asyncio.fixture(scope="function")
async def db_postgis(db_postgis_module):
    """Fixture for PostGISField/STDistance/STDWithin tests, with transaction rollback cleanup
    between tests."""
    async with RollbackIsolation(db_postgis_module) as ctx:
        yield ctx


@pytest_asyncio.fixture(scope="module")
async def db_citext_module():
    """
    Module-scoped fixture for CitextField tests - creates the database, schema and citext
    extension ONCE per file instead of once per test.

    Uses models defined in tests.dialects.postgresql.models_citext module. Same sequence as
    db_postgis_module() (CREATE EXTENSION run manually between database creation and schema
    generation - CitextField's column type doesn't exist until that runs), since the citext
    extension is available on plain postgres images (unlike postgis, this fixture doesn't skip
    on a missing extension).
    """
    skip_if_not_postgres()
    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    async with hare_test_context(
        modules=["tests.dialects.postgresql.models_citext"],
        db_url=db_url,
        app_label="models",
        connection_label="models",
        _generate_schemas=False,
    ) as ctx:
        await ctx.get_connection().execute_script("CREATE EXTENSION IF NOT EXISTS citext;")
        await ctx.generate_schemas(safe=False)
        yield ctx


@pytest_asyncio.fixture(scope="function")
async def db_citext(db_citext_module):
    """Fixture for CitextField tests, with transaction rollback cleanup between tests."""
    async with RollbackIsolation(db_citext_module) as ctx:
        yield ctx


@pytest_asyncio.fixture(scope="module")
async def db_network_module():
    """Module-scoped database of the inet/cidr/macaddr field models."""
    skip_if_not_postgres()
    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    async with hare_test_context(
        modules=["tests.dialects.postgresql.models_network"],
        db_url=db_url,
        app_label="models",
        connection_label="models",
    ) as ctx:
        yield ctx


@pytest_asyncio.fixture(scope="function")
async def db_network(db_network_module):
    """Fixture for the network field tests, with transaction rollback cleanup between tests."""
    async with RollbackIsolation(db_network_module) as ctx:
        yield ctx


@pytest_asyncio.fixture(scope="module")
async def db_multi_range_module():
    """Module-scoped database of the multirange field models."""
    skip_if_not_postgres()
    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    async with hare_test_context(
        modules=["tests.dialects.postgresql.models_multi_range"],
        db_url=db_url,
        app_label="models",
        connection_label="models",
    ) as ctx:
        yield ctx


@pytest_asyncio.fixture(scope="function")
async def db_multi_range(db_multi_range_module):
    """Fixture for the multirange field tests, with transaction rollback cleanup between tests."""
    async with RollbackIsolation(db_multi_range_module) as ctx:
        yield ctx


@pytest_asyncio.fixture(scope="module")
async def db_ltree_module():
    """Module-scoped database of the ltree field models - generate_schemas() creates the extension."""
    skip_if_not_postgres()
    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    async with hare_test_context(
        modules=["tests.dialects.postgresql.models_ltree"],
        db_url=db_url,
        app_label="models",
        connection_label="models",
    ) as ctx:
        yield ctx


@pytest_asyncio.fixture(scope="function")
async def db_ltree(db_ltree_module):
    """Fixture for the ltree field tests, with transaction rollback cleanup between tests."""
    async with RollbackIsolation(db_ltree_module) as ctx:
        yield ctx


@pytest_asyncio.fixture(scope="module")
async def db_native_enum_module():
    """Module-scoped database of the NativeEnumField models - generate_schemas() creates their ENUM
    types before the tables."""
    skip_if_not_postgres()
    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    async with hare_test_context(
        modules=["tests.dialects.postgresql.models_native_enum"],
        db_url=db_url,
        app_label="models",
        connection_label="models",
    ) as ctx:
        yield ctx


@pytest_asyncio.fixture(scope="function")
async def db_native_enum(db_native_enum_module):
    """Fixture for NativeEnumField tests, with transaction rollback cleanup between tests."""
    async with RollbackIsolation(db_native_enum_module) as ctx:
        yield ctx


@pytest_asyncio.fixture(scope="module")
async def db_schema_objects_module():
    """Module-scoped database of a model declaring a view, a materialized view, a function, a
    sequence, row level security, a policy and grants - created by generate_schemas() after the
    role the grants are for."""
    skip_if_not_postgres()
    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    async with hare_test_context(
        modules=["tests.dialects.postgresql.models_schema_objects"],
        db_url=db_url,
        app_label="models",
        connection_label="models",
        _generate_schemas=False,
    ) as ctx:
        # A role belongs to the whole server - made once, kept for the other runs.
        await ctx.get_connection().execute_script(
            "DO $role$ BEGIN CREATE ROLE hare_schema_object_reader; "
            "EXCEPTION WHEN duplicate_object OR unique_violation THEN NULL; END $role$;"
        )
        await ctx.generate_schemas(safe=False)
        yield ctx


@pytest_asyncio.fixture(scope="function")
async def db_schema_objects(db_schema_objects_module):
    """Fixture for the schema object tests, with transaction rollback cleanup between tests."""
    async with RollbackIsolation(db_schema_objects_module) as ctx:
        yield ctx


@pytest_asyncio.fixture(scope="module")
async def db_agg_module():
    """
    Module-scoped fixture for ArrayAgg/StringAgg/Trunc*/Extract* tests.

    Uses models defined in tests.dialects.postgresql.models_agg module.
    """
    skip_if_not_postgres()
    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    async with hare_test_context(
        modules=["tests.dialects.postgresql.models_agg"],
        db_url=db_url,
        app_label="models",
        connection_label="models",
    ) as ctx:
        yield ctx


@pytest_asyncio.fixture(scope="function")
async def db_agg(db_agg_module):
    """Function-scoped fixture with transaction rollback, for ArrayAgg/StringAgg/Trunc*/Extract*."""
    async with RollbackIsolation(db_agg_module) as ctx:
        yield ctx


@pytest_asyncio.fixture(scope="module")
async def db_ranges_module():
    """Module-scoped fixture for RangeField tests. Uses models defined in
    tests.dialects.postgresql.models_ranges module."""
    skip_if_not_postgres()
    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    async with hare_test_context(
        modules=["tests.dialects.postgresql.models_ranges"],
        db_url=db_url,
        app_label="models",
        connection_label="models",
    ) as ctx:
        yield ctx


@pytest_asyncio.fixture(scope="function")
async def db_ranges(db_ranges_module):
    """Function-scoped fixture with transaction rollback, for RangeField tests."""
    async with RollbackIsolation(db_ranges_module) as ctx:
        yield ctx


@pytest_asyncio.fixture(scope="module")
async def db_container_paths_module():
    """Module-scoped fixture for array/range path tests, on tests.dialects.postgresql.models_container_paths."""
    skip_if_not_postgres()
    async with hare_test_context(
        modules=["tests.dialects.postgresql.models_container_paths"],
        db_url=os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:"),
        app_label="models",
        connection_label="models",
    ) as ctx:
        yield ctx


@pytest_asyncio.fixture(scope="function")
async def db_container_paths(db_container_paths_module):
    """Function-scoped fixture with transaction rollback, for array/range path tests."""
    async with RollbackIsolation(db_container_paths_module) as ctx:
        yield ctx


@pytest_asyncio.fixture(scope="module")
async def db_vector_module():
    """
    Module-scoped fixture for VectorField/L2Distance/CosineDistance/InnerProduct tests - creates
    the database, schema and vector extension ONCE per file instead of once per test.

    Uses models defined in tests.dialects.postgresql.models_vector module. Same sequence as
    db_postgis_module() (CREATE EXTENSION run manually between database creation and schema
    generation - VectorField's column type doesn't exist until that runs). Skips (not fails) if
    the extension isn't installable, since most Postgres test setups don't have pgvector
    installed (needs the pgvector/pgvector image, not plain postgres).
    """
    skip_if_not_postgres()
    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    async with hare_test_context(
        modules=["tests.dialects.postgresql.models_vector"],
        db_url=db_url,
        app_label="models",
        connection_label="models",
        _generate_schemas=False,
    ) as ctx:
        try:
            await ctx.get_connection().execute_script("CREATE EXTENSION IF NOT EXISTS vector;")
        except Exception as exc:
            pytest.skip(f"vector extension not available: {exc}")
        await ctx.generate_schemas(safe=False)
        yield ctx


@pytest_asyncio.fixture(scope="function")
async def db_vector(db_vector_module):
    """Fixture for VectorField/L2Distance/CosineDistance/InnerProduct tests, with transaction
    rollback cleanup between tests."""
    async with RollbackIsolation(db_vector_module) as ctx:
        yield ctx
