"""
Custom fixtures for PostgreSQL-specific tests that require specific model modules.

These fixtures support tests that define hare_test_modules to use
custom model definitions for PostgreSQL features like TSVector.
"""

import os
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio

from hare.contrib.test.helpers import hare_test_context
from hare.core.context import HareContext


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
    quote_char = next(iter(ctx.apps.get_models_iterable()))._meta.db.query_class.SQL_CONTEXT.quote_char
    quoted_tables = ", ".join(f"{quote_char}{table}{quote_char}" for table in tables)
    await ctx.db().execute_script(f"TRUNCATE TABLE {quoted_tables} RESTART IDENTITY CASCADE")  # nosec


def skip_if_not_postgres():
    """Skip test if not running against PostgreSQL."""
    db_url = os.getenv("HARE_TEST_DB", "")
    if db_url.split("://", 1)[0].split("+", 1)[0] != "postgresql":
        pytest.skip("Postgres-only test.")


async def _rollback_wrapped(ctx: HareContext) -> AsyncGenerator[HareContext]:
    """Shared transaction-rollback body for the function-scoped fixtures below - each wraps a
    module-scoped "create once per file" context so per-test cleanup is a rollback instead of a
    repeated full database create/schema/drop cycle."""
    conn = ctx.db()
    transaction = conn._in_transaction()
    await transaction.__aenter__()
    try:
        yield ctx
    finally:

        class _RollbackException(Exception):
            pass

        await transaction.__aexit__(_RollbackException, _RollbackException(), None)


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
    async for ctx in _rollback_wrapped(db_module_postgres):
        yield ctx


@pytest_asyncio.fixture(scope="module")
async def db_tsvector_module():
    """Module-scoped fixture for TestTSVectorField/TestPostgresSearchLookupTSVector - both share
    the same tests.dialects.postgresql.models_tsvector module, so both db_tsvector and db_search
    build on this one create-once-per-file database instead of each recreating it per test."""
    skip_if_not_postgres()
    db_url = os.getenv("HARE_TEST_DB", "sqlite://:memory:")
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
    async for ctx in _rollback_wrapped(db_tsvector_module):
        yield ctx


@pytest_asyncio.fixture(scope="function")
async def db_search(db_tsvector_module):
    """
    Fixture for TestPostgresSearchLookupTSVector, with transaction rollback cleanup between
    tests - shares db_tsvector_module's already-created database/schema (same underlying
    tests.dialects.postgresql.models_tsvector module as db_tsvector).

    Equivalent to: test.IsolatedTestCase with hare_test_modules
    """
    async for ctx in _rollback_wrapped(db_tsvector_module):
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
    db_url = os.getenv("HARE_TEST_DB", "sqlite://:memory:")
    async with hare_test_context(
        modules=["tests.dialects.postgresql.models_postgis"],
        db_url=db_url,
        app_label="models",
        connection_label="models",
        _generate_schemas=False,
    ) as ctx:
        try:
            await ctx.db().execute_script("CREATE EXTENSION IF NOT EXISTS postgis;")
        except Exception as exc:
            pytest.skip(f"postgis extension not available: {exc}")
        await ctx.generate_schemas(safe=False)
        yield ctx


@pytest_asyncio.fixture(scope="function")
async def db_postgis(db_postgis_module):
    """Fixture for PostGISField/STDistance/STDWithin tests, with transaction rollback cleanup
    between tests."""
    async for ctx in _rollback_wrapped(db_postgis_module):
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
    db_url = os.getenv("HARE_TEST_DB", "sqlite://:memory:")
    async with hare_test_context(
        modules=["tests.dialects.postgresql.models_citext"],
        db_url=db_url,
        app_label="models",
        connection_label="models",
        _generate_schemas=False,
    ) as ctx:
        await ctx.db().execute_script("CREATE EXTENSION IF NOT EXISTS citext;")
        await ctx.generate_schemas(safe=False)
        yield ctx


@pytest_asyncio.fixture(scope="function")
async def db_citext(db_citext_module):
    """Fixture for CitextField tests, with transaction rollback cleanup between tests."""
    async for ctx in _rollback_wrapped(db_citext_module):
        yield ctx


@pytest_asyncio.fixture(scope="module")
async def db_agg_module():
    """
    Module-scoped fixture for ArrayAgg/StringAgg/Trunc*/Extract* tests.

    Uses models defined in tests.dialects.postgresql.models_agg module.
    """
    skip_if_not_postgres()
    db_url = os.getenv("HARE_TEST_DB", "sqlite://:memory:")
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
    async for ctx in _rollback_wrapped(db_agg_module):
        yield ctx


@pytest_asyncio.fixture(scope="module")
async def db_ranges_module():
    """Module-scoped fixture for RangeField tests. Uses models defined in
    tests.dialects.postgresql.models_ranges module."""
    skip_if_not_postgres()
    db_url = os.getenv("HARE_TEST_DB", "sqlite://:memory:")
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
    async for ctx in _rollback_wrapped(db_ranges_module):
        yield ctx


@pytest_asyncio.fixture(scope="module")
async def db_container_paths_module():
    """Module-scoped fixture for array/range path tests, on tests.dialects.postgresql.models_container_paths."""
    skip_if_not_postgres()
    async with hare_test_context(
        modules=["tests.dialects.postgresql.models_container_paths"],
        db_url=os.getenv("HARE_TEST_DB", "sqlite://:memory:"),
        app_label="models",
        connection_label="models",
    ) as ctx:
        yield ctx


@pytest_asyncio.fixture(scope="function")
async def db_container_paths(db_container_paths_module):
    """Function-scoped fixture with transaction rollback, for array/range path tests."""
    async for ctx in _rollback_wrapped(db_container_paths_module):
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
    db_url = os.getenv("HARE_TEST_DB", "sqlite://:memory:")
    async with hare_test_context(
        modules=["tests.dialects.postgresql.models_vector"],
        db_url=db_url,
        app_label="models",
        connection_label="models",
        _generate_schemas=False,
    ) as ctx:
        try:
            await ctx.db().execute_script("CREATE EXTENSION IF NOT EXISTS vector;")
        except Exception as exc:
            pytest.skip(f"vector extension not available: {exc}")
        await ctx.generate_schemas(safe=False)
        yield ctx


@pytest_asyncio.fixture(scope="function")
async def db_vector(db_vector_module):
    """Fixture for VectorField/L2Distance/CosineDistance/InnerProduct tests, with transaction
    rollback cleanup between tests."""
    async for ctx in _rollback_wrapped(db_vector_module):
        yield ctx
