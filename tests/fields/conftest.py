"""
Custom fixtures for field tests that require specific model modules.

These fixtures support tests that define hare_test_modules to use
custom model definitions instead of the default tests.testmodels.
"""

import os
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio

from hare.contrib.test import truncate_all_models
from hare.contrib.test.helpers import hare_test_context
from hare.core.context import HareContext
from tests.utils.database_under_test import DatabaseUnderTest


async def _rollback_wrapped(ctx: HareContext) -> AsyncGenerator[HareContext]:
    """Shared transaction-rollback body for the function-scoped fixtures below - each wraps a
    module-scoped "create once per file" context so per-test cleanup is a rollback instead of a
    repeated full database create/schema/drop cycle."""
    conn = ctx.db()
    if not conn.features.supports_transactions:
        # No transaction to scope the test to this context's connection - the context itself is
        # made current for it instead.
        token = HareContext.current_context.set(ctx)
        try:
            yield ctx
        finally:
            HareContext.current_context.reset(token)
        await truncate_all_models(context=ctx)
        return
    transaction = conn._in_transaction()
    await transaction.__aenter__()
    try:
        yield ctx
    finally:

        class _RollbackException(Exception):
            pass

        await transaction.__aexit__(_RollbackException, _RollbackException(), None)


@pytest_asyncio.fixture(scope="module")
async def db_array_fields_module():
    """Module-scoped fixture for TestArrayFields - creates the database and schema ONCE per file
    instead of once per test.

    Uses models defined in tests.testmodels_postgres module.
    """
    db_url = os.getenv("HARE_TEST_DB", "sqlite://:memory:")
    if DatabaseUnderTest.get_dialect().name != "postgresql":
        pytest.skip("ArrayFields require PostgreSQL")
    async with hare_test_context(
        modules=["tests.testmodels_postgres"],
        db_url=db_url,
        app_label="models",
        connection_label="models",
    ) as ctx:
        yield ctx


@pytest_asyncio.fixture(scope="function")
async def db_array_fields(db_array_fields_module):
    """
    Fixture for TestArrayFields, with transaction rollback cleanup between tests.

    Equivalent to: test.IsolatedTestCase with hare_test_modules=["tests.testmodels_postgres"]
    """
    async for ctx in _rollback_wrapped(db_array_fields_module):
        yield ctx


@pytest_asyncio.fixture(scope="module")
async def db_generated_field_module():
    """Module-scoped fixture for GeneratedField tests (STORED, dialect-agnostic) - creates the
    database and schema ONCE per file instead of once per test.

    Uses models defined in tests.fields.models_generated_field module.
    """
    db_url = os.getenv("HARE_TEST_DB", "sqlite://:memory:")
    async with hare_test_context(
        modules=["tests.fields.models_generated_field"],
        db_url=db_url,
        app_label="models",
        connection_label="models",
    ) as ctx:
        yield ctx


@pytest_asyncio.fixture(scope="function")
async def db_generated_field(db_generated_field_module):
    """Fixture for GeneratedField tests (STORED, dialect-agnostic), with transaction rollback
    cleanup between tests."""
    async for ctx in _rollback_wrapped(db_generated_field_module):
        yield ctx


@pytest_asyncio.fixture(scope="module")
async def db_generated_field_virtual_module():
    """Module-scoped fixture for GeneratedField(stored=False) tests - creates the database and
    schema ONCE per file instead of once per test.

    Uses models defined in tests.fields.models_generated_field_virtual module. Skipped on
    Postgres - Postgres only supports STORED generated columns, not VIRTUAL.
    """
    db_url = os.getenv("HARE_TEST_DB", "sqlite://:memory:")
    if not DatabaseUnderTest.get_dialect().supports_virtual_generated_columns:
        pytest.skip("The database has no VIRTUAL generated columns (Postgres only supports STORED).")
    async with hare_test_context(
        modules=["tests.fields.models_generated_field_virtual"],
        db_url=db_url,
        app_label="models",
        connection_label="models",
    ) as ctx:
        yield ctx


@pytest_asyncio.fixture(scope="function")
async def db_generated_field_virtual(db_generated_field_virtual_module):
    """Fixture for GeneratedField(stored=False) tests, with transaction rollback cleanup
    between tests."""
    async for ctx in _rollback_wrapped(db_generated_field_virtual_module):
        yield ctx


@pytest_asyncio.fixture(scope="module")
async def db_subclass_fields_module():
    """Module-scoped fixture for TestEnumField and TestCustomFieldFilters - creates the database
    and schema ONCE per file instead of once per test.

    Uses models defined in tests.fields.subclass_models module.
    """
    db_url = os.getenv("HARE_TEST_DB", "sqlite://:memory:")
    async with hare_test_context(
        modules=["tests.fields.subclass_models"],
        db_url=db_url,
        app_label="models",
        connection_label="models",
    ) as ctx:
        yield ctx


@pytest_asyncio.fixture(scope="function")
async def db_subclass_fields(db_subclass_fields_module):
    """
    Fixture for TestEnumField and TestCustomFieldFilters, with transaction rollback cleanup
    between tests.

    Equivalent to: test.IsolatedTestCase with hare_test_modules=["tests.fields.subclass_models"]
    """
    async for ctx in _rollback_wrapped(db_subclass_fields_module):
        yield ctx


@pytest_asyncio.fixture(scope="module")
async def db_encrypted_fields_module():
    """Module-scoped fixture for the encrypted-field tests (tests.fields.models_encrypted)."""
    db_url = os.getenv("HARE_TEST_DB", "sqlite://:memory:")
    async with hare_test_context(
        modules=["tests.fields.models_encrypted"],
        db_url=db_url,
        app_label="models",
        connection_label="models",
    ) as ctx:
        yield ctx


@pytest_asyncio.fixture(scope="function")
async def db_encrypted_fields(db_encrypted_fields_module):
    """db_encrypted_fields_module with transaction rollback cleanup between tests."""
    async for ctx in _rollback_wrapped(db_encrypted_fields_module):
        yield ctx


@pytest_asyncio.fixture(scope="module")
async def db_write_paths_module():
    """Module-scoped fixture for the write-path parity tests (tests.fields.models_write_paths)."""
    db_url = os.getenv("HARE_TEST_DB", "sqlite://:memory:")
    async with hare_test_context(
        modules=["tests.fields.models_write_paths"],
        db_url=db_url,
        app_label="models",
        connection_label="models",
    ) as ctx:
        yield ctx


@pytest_asyncio.fixture(scope="function")
async def db_write_paths(db_write_paths_module):
    """db_write_paths_module with transaction rollback cleanup between tests."""
    async for ctx in _rollback_wrapped(db_write_paths_module):
        yield ctx
