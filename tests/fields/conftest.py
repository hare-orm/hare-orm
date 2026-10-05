"""
Custom fixtures for field tests that require specific model modules.

These fixtures support tests that define hare_test_modules to use
custom model definitions instead of the default tests.testmodels.
"""

import os

import pytest
import pytest_asyncio

from hare.contrib.test import RollbackIsolation
from hare.contrib.test.isolated_contexts import hare_test_context
from tests.utils.database_under_test import DatabaseUnderTest


@pytest_asyncio.fixture(scope="module")
async def db_array_fields_module():
    """Module-scoped fixture for TestArrayFields - creates the database and schema ONCE per file
    instead of once per test.

    Uses models defined in tests.testmodels_postgres module.
    """
    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
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
    async with RollbackIsolation(db_array_fields_module) as ctx:
        yield ctx


@pytest_asyncio.fixture(scope="module")
async def db_generated_field_module():
    """Module-scoped fixture for GeneratedField tests (STORED, dialect-agnostic) - creates the
    database and schema ONCE per file instead of once per test.

    Uses models defined in tests.fields.models_generated_field module.
    """
    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
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
    async with RollbackIsolation(db_generated_field_module) as ctx:
        yield ctx


@pytest_asyncio.fixture(scope="module")
async def db_generated_field_virtual_module():
    """Module-scoped fixture for GeneratedField(stored=False) tests - creates the database and
    schema ONCE per file instead of once per test.

    Uses models defined in tests.fields.models_generated_field_virtual module. Skipped on a server
    without VIRTUAL generated columns (PostgreSQL before 18).
    """
    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    async with hare_test_context(
        modules=["tests.fields.models_generated_field_virtual"],
        db_url=db_url,
        app_label="models",
        connection_label="models",
        _generate_schemas=False,
    ) as ctx:
        # Connected first, so the features follow the server's own version.
        async with ctx.get_connection().acquire_connection():
            pass
        if not ctx.get_connection().features.supports_virtual_generated_columns:
            pytest.skip("The server has no VIRTUAL generated columns (PostgreSQL before 18).")
        await ctx.generate_schemas(safe=False)
        yield ctx


@pytest_asyncio.fixture(scope="function")
async def db_generated_field_virtual(db_generated_field_virtual_module):
    """Fixture for GeneratedField(stored=False) tests, with transaction rollback cleanup
    between tests."""
    async with RollbackIsolation(db_generated_field_virtual_module) as ctx:
        yield ctx


@pytest_asyncio.fixture(scope="module")
async def db_subclass_fields_module():
    """Module-scoped fixture for TestEnumField and TestCustomFieldFilters - creates the database
    and schema ONCE per file instead of once per test.

    Uses models defined in tests.fields.subclass_models module.
    """
    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
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
    async with RollbackIsolation(db_subclass_fields_module) as ctx:
        yield ctx


@pytest_asyncio.fixture(scope="module")
async def db_encrypted_fields_module():
    """Module-scoped fixture for the encrypted-field tests (tests.fields.models_encrypted)."""
    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    async with hare_test_context(
        modules=["tests.fields.models_encrypted"],
        db_url=db_url,
        app_label="models",
        connection_label="models",
    ) as ctx:
        yield ctx


@pytest_asyncio.fixture(scope="module")
async def db_blind_index_module():
    """Module-scoped fixture for the blind index tests (tests.fields.models_blind_index)."""
    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    async with hare_test_context(
        modules=["tests.fields.models_blind_index"],
        db_url=db_url,
        app_label="models",
        connection_label="models",
    ) as ctx:
        yield ctx


@pytest_asyncio.fixture(scope="function")
async def db_blind_index(db_blind_index_module):
    """db_blind_index_module with transaction rollback cleanup between tests."""
    async with RollbackIsolation(db_blind_index_module) as ctx:
        yield ctx


@pytest_asyncio.fixture(scope="function")
async def db_encrypted_fields(db_encrypted_fields_module):
    """db_encrypted_fields_module with transaction rollback cleanup between tests."""
    async with RollbackIsolation(db_encrypted_fields_module) as ctx:
        yield ctx


@pytest_asyncio.fixture(scope="module")
async def db_write_paths_module():
    """Module-scoped fixture for the write-path parity tests (tests.fields.models_write_paths)."""
    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
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
    async with RollbackIsolation(db_write_paths_module) as ctx:
        yield ctx


@pytest_asyncio.fixture(scope="module")
async def db_text_formats_module():
    """Module-scoped fixture for the text format field tests (tests.fields.models_text_formats)."""
    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    async with hare_test_context(
        modules=["tests.fields.models_text_formats"],
        db_url=db_url,
        app_label="models",
        connection_label="models",
    ) as ctx:
        yield ctx


@pytest_asyncio.fixture(scope="function")
async def db_text_formats(db_text_formats_module):
    """db_text_formats_module with transaction rollback cleanup between tests."""
    async with RollbackIsolation(db_text_formats_module) as ctx:
        yield ctx


@pytest_asyncio.fixture(scope="module")
async def db_generic_fk_module():
    """Module-scoped fixture for the generic foreign key tests (tests.fields.models_generic_fk)."""
    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    async with hare_test_context(
        modules=["tests.fields.models_generic_fk"],
        db_url=db_url,
        app_label="models",
        connection_label="models",
    ) as ctx:
        yield ctx


@pytest_asyncio.fixture(scope="function")
async def db_generic_fk(db_generic_fk_module):
    """db_generic_fk_module with transaction rollback cleanup between tests."""
    async with RollbackIsolation(db_generic_fk_module) as ctx:
        yield ctx
