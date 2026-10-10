from __future__ import annotations

from hare.contrib.test.constants import MEMORY_SQLITE

#: The pytest ini setting naming the configuration - a dotted path, as ``[tool.hare] hare_orm``.
HARE_CONFIG_INI = "hare_config"
#: The pytest ini setting and command line option naming the URL the test databases are made from.
HARE_DB_URL_INI = "hare_db_url"
HARE_DB_URL_OPTION = "--hare-db-url"
#: The command line option leasing the test databases from ``ReusableTestDatabases``.
HARE_REUSE_DB_OPTION = "--hare-reuse-db"
#: The URL the test databases are made from when nothing names one.
DEFAULT_TEST_DB_URL = MEMORY_SQLITE
#: The marker skipping a test unless its connection has the features named.
HARE_REQUIRES_MARKER = "hare_requires"
HARE_REQUIRES_MARKER_HELP = (
    "hare_requires(connection_alias=None, **conditions): skip the test unless the connection's features match"
)
#: The fixtures giving a test its context - the marker checks the connection of the first one used.
CONTEXT_FIXTURE_NAMES = ("hare_db", "hare_transactional_db", "hare_database")
#: Why the fixtures can't run without pytest-asyncio.
PYTEST_ASYNCIO_MISSING_MESSAGE = "hare's pytest fixtures run on pytest-asyncio - install hare-orm[pytest]"
#: The module the plugin registers its fixtures from, missing without the extra.
PYTEST_ASYNCIO_MODULE = "pytest_asyncio"
#: The names the plugin registers its fixtures under in pytest's plugin manager.
HARE_FIXTURES_PLUGIN_NAME = "hare-fixtures"
