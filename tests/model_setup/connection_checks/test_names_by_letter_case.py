"""On a database taking names differing only in the case of their letters for one name (SQLite), two such
tables, columns or indexes are refused when the models are bound: the second table was the first one, the
second column a duplicate, the second unique index never created - its duplicates accepted."""

import os

import pytest

from hare.contrib.test import hare_test_context
from hare.exceptions import ConfigurationError, IntegrityError
from tests.model_setup.connection_checks.models_letter_case_unicode import LowerName, UpperName

PACKAGE = "tests.model_setup.connection_checks"
DECLARATIONS = [
    ("models_letter_case_tables", "tables 'Case_w1' and 'case_w1'"),
    ("models_letter_case_columns", "columns 'Name' and 'name'"),
    ("models_letter_case_indexes", "constraints or indexes 'Uq_N6' and 'uq_n6'"),
]


def get_db_url() -> str:
    return os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")


def in_context(module: str, **options):
    return hare_test_context(modules=[f"{PACKAGE}.{module}"], db_url=get_db_url(), app_label="models", **options)


@pytest.mark.parametrize(("module", "message"), DECLARATIONS)
@pytest.mark.asyncio
async def test_names_differing_only_in_letter_case_are_refused_where_the_database_ignores_it(module, message):
    if not get_db_url().startswith("sqlite"):
        pytest.skip("SQLite takes such names for one")
    with pytest.raises(ConfigurationError, match=message):
        async with in_context(module, _generate_schemas=False):
            pass


@pytest.mark.parametrize(("module", "message"), DECLARATIONS)
@pytest.mark.asyncio
async def test_names_differing_only_in_letter_case_are_two_where_the_database_tells_them_apart(module, message):
    if get_db_url().split(":", 1)[0].split("+", 1)[0] != "postgresql":
        pytest.skip("PostgreSQL tells quoted names of another case apart")
    async with in_context(module):
        pass


@pytest.mark.sqlite_only
@pytest.mark.asyncio
async def test_non_ascii_names_keep_the_distinctions_the_database_preserves():
    async with in_context("models_letter_case_unicode"):
        await UpperName.objects.create(id=1, first="first", second="second")
        await LowerName.objects.create(id=1, value=7)
        assert await UpperName.objects.values("first", "second") == [{"first": "first", "second": "second"}]
        assert await LowerName.objects.values_list("value", flat=True) == [7]
        with pytest.raises(IntegrityError):
            await UpperName.objects.create(id=2, first="first", second="other")
        with pytest.raises(IntegrityError):
            await UpperName.objects.create(id=3, first="other", second="second")
