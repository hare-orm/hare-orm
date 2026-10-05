"""hare's pytest plugin, loaded through the ``pytest11`` entry point. pytest calls a plugin's hooks by
their names - these two add the plugin's settings and register its fixtures (``HareFixtures``)."""

from __future__ import annotations

import pytest

from hare.contrib.pytest.constants import (
    HARE_CONFIG_INI,
    HARE_DB_URL_INI,
    HARE_DB_URL_OPTION,
    HARE_FIXTURES_PLUGIN_NAME,
    HARE_REQUIRES_MARKER_HELP,
    HARE_REUSE_DB_OPTION,
    PYTEST_ASYNCIO_MODULE,
)


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("hare")
    group.addoption(
        HARE_DB_URL_OPTION,
        dest="hare_db_url",
        default=None,
        help="The URL the test databases are made from - a '{}' in the database name gets a fresh name.",
    )
    group.addoption(
        HARE_REUSE_DB_OPTION,
        dest="hare_reuse_db",
        action="store_true",
        default=False,
        help="Lease the test databases from ReusableTestDatabases instead of creating and dropping them.",
    )
    parser.addini(HARE_CONFIG_INI, "The dotted path of hare's configuration, as [tool.hare] hare_orm.", default="")
    parser.addini(HARE_DB_URL_INI, "The URL the test databases are made from.", default="")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", HARE_REQUIRES_MARKER_HELP)
    try:
        from hare.contrib.pytest.hare_fixtures import HareFixtures
    except ModuleNotFoundError as error:
        if error.name != PYTEST_ASYNCIO_MODULE:
            raise
        from hare.contrib.pytest.missing_pytest_asyncio_fixtures import MissingPytestAsyncioFixtures

        config.pluginmanager.register(MissingPytestAsyncioFixtures(), HARE_FIXTURES_PLUGIN_NAME)
        return
    config.pluginmanager.register(HareFixtures(), HARE_FIXTURES_PLUGIN_NAME)
