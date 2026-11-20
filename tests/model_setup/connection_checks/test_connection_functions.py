"""A declaration the database keeps in its DDL - an index key, a generated column - calling a function only
hare's connections have (``Greatest()`` on SQLite is ``hare_greatest``) is refused when the model is bound:
makemigrations wrote a migration of it that migrate couldn't apply."""

import pytest

from hare.contrib.test import hare_test_context
from hare.exceptions import UnSupportedError
from tests.model_setup.connection_checks.models_connection_functions import GeneratedFloor, IndexedFloor
from tests.utils.database_under_test import DatabaseUnderTest

MODULE = "tests.model_setup.connection_checks.models_connection_functions"


@pytest.mark.asyncio
async def test_a_declaration_calling_a_function_of_hare_s_connections_is_refused_when_bound():
    if not DatabaseUnderTest.get_url().startswith("sqlite"):
        pytest.skip("only SQLite has Greatest() on hare's own connections alone")
    with pytest.raises(UnSupportedError, match="hare_greatest"):
        async with hare_test_context(
            modules=[MODULE], db_url=DatabaseUnderTest.get_url(), app_label="models", _generate_schemas=False
        ):
            pass


@pytest.mark.asyncio
async def test_the_declarations_work_where_the_database_has_the_function():
    if DatabaseUnderTest.get_url().split(":", 1)[0].split("+", 1)[0] != "postgresql":
        pytest.skip("PostgreSQL has GREATEST of its own")
    async with hare_test_context(modules=[MODULE], db_url=DatabaseUnderTest.get_url(), app_label="models"):
        await GeneratedFloor.objects.create(id=1, qty=1)
        await IndexedFloor.objects.create(id=1, qty=5)
        assert await GeneratedFloor.objects.values_list("floor", flat=True) == [3]
