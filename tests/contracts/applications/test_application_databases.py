"""Application x test fixture matrix: the application of each integration - Robyn, Litestar, FastAPI, taskiq -
runs on the databases of hare's pytest plugin, the cells of an integration started in a pytest run of their own.
Under ``hare_app_db`` a write through it goes into the test's transaction - seen by the test and rolled back with
it; an application in a loop of its own is refused there on a driver binding its connections to the test's loop
(asyncpg). Under ``hare_app_transactional_db`` a write is seen by the test and the tables are empty in the next
test; stopping the application leaves the test's context open. The configured database doesn't exist - the plugin
must not open it. On a SQLite file of the test - in the database-independent suite - and on the PostgreSQL server
of the suite run."""

import os

import pytest

from tests.contracts.applications.application_cells import APPLICATION_CELLS_BY_INTEGRATION
from tests.contracts.constants import PRINTED_TEST_OUTCOME_PATTERN
from tests.utils.database_under_test import DatabaseUnderTest

pytest_plugins = ["pytester"]

MODELS = """
from hare import fields
from hare.models import Model


class Note(Model):
    id = fields.IntField(primary_key=True)
    text = fields.CharField(max_length=50)
"""
SETTINGS = """
HARE_ORM = {
    "connections": {"default": "postgresql://nobody@nowhere.invalid/production"},
    "apps": {"models": {"models": ["plugin_models"], "default_connection": "default"}},
}
"""
INI = """
[pytest]
hare_config = plugin_settings.HARE_ORM
asyncio_mode = strict
asyncio_default_fixture_loop_scope = session
asyncio_default_test_loop_scope = session
"""


@pytest.mark.parametrize("database", [pytest.param("sqlite", marks=pytest.mark.database_independent), "postgresql"])
@pytest.mark.parametrize(
    "cells",
    [
        pytest.param(cells, id=integration, marks=[pytest.mark.requires(*cells[0].requires)])
        for integration, cells in APPLICATION_CELLS_BY_INTEGRATION.items()
    ],
)
def test_the_applications_of_an_integration_run_on_the_test_databases(pytester, monkeypatch, cells, database):
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    if database == "postgresql":
        database_url = os.getenv("HARE_TEST_DB", "")
        if not database_url.startswith("postgresql"):
            pytest.skip("HARE_TEST_DB names no PostgreSQL server")
    else:
        database_url = f"sqlite+aiosqlite:///{(pytester.path / 'application.sqlite3').as_posix().lstrip('/')}"
    pytester.makepyfile(plugin_models=MODELS, plugin_settings=SETTINGS)
    pytester.makeini(INI)
    pytester.makepyfile(**{cell.get_module_name(): cell.get_source() for cell in cells})
    result = pytester.runpytest_subprocess(
        "-p", "no:randomly", "-p", "no:cacheprovider", "-v", f"--hare-db-url={database_url}"
    )
    bound = DatabaseUnderTest.get_driver(database_url).get_client_class({}).connections_bound_to_event_loop
    printed = {
        (module_name, test_name): outcome
        for module_name, test_name, outcome in PRINTED_TEST_OUTCOME_PATTERN.findall(result.stdout.str())
    }
    expected = {
        (cell.get_module_name(), test_name): outcome
        for cell in cells
        for test_name, outcome in cell.get_expected_outcomes(bound).items()
    }
    assert printed == expected, result.stdout.str()
