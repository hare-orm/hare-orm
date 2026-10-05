from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from typing import Any

import pytest
import pytest_asyncio

from hare.cli.config_locator import ConfigLocator
from hare.contrib.pytest.constants import (
    CONTEXT_FIXTURE_NAMES,
    DEFAULT_TEST_DB_URL,
    HARE_CONFIG_INI,
    HARE_DB_URL_INI,
    HARE_REQUIRES_MARKER,
)
from hare.contrib.test.databases.model_truncation import truncate_all_models
from hare.contrib.test.databases.rollback_isolation import RollbackIsolation
from hare.contrib.test.databases.temporary_databases import TemporaryDatabases
from hare.contrib.test.features.feature_conditions import FeatureConditions
from hare.contrib.test.queries.query_counting import assert_query_count
from hare.core.config import HareConfig
from hare.core.hare_context import HareContext


class HareFixtures:
    """The fixtures of hare's pytest plugin and its ``hare_requires`` marker. The test databases are
    made from ``hare_db_url`` (in-memory SQLite by default) for every connection of the
    configuration - the configured databases themselves are never opened."""

    @staticmethod
    def get_config(pytestconfig: pytest.Config) -> dict[str, Any]:
        """The configuration the ini's ``hare_config`` names, else ``HARE_ORM`` or ``[tool.hare]``.

        Raises:
            pytest.UsageError: Nothing names one.
        """
        config_path = pytestconfig.getini(HARE_CONFIG_INI) or ConfigLocator.locate()
        if not config_path:
            raise pytest.UsageError(
                f"hare's fixtures need the configuration: set {HARE_CONFIG_INI} in the pytest settings, "
                "HARE_ORM in the environment or hare_orm in pyproject.toml's [tool.hare]"
            )
        return HareConfig.load(config_path).to_dict()

    @staticmethod
    def get_db_url(pytestconfig: pytest.Config) -> str:
        """The URL the test databases are made from - the command line's, else the ini's."""
        return pytestconfig.getoption("hare_db_url") or pytestconfig.getini(HARE_DB_URL_INI) or DEFAULT_TEST_DB_URL

    @pytest_asyncio.fixture(scope="session", loop_scope="session")
    async def hare_database(self, pytestconfig: pytest.Config) -> AsyncGenerator[HareContext]:
        """The test databases with the models' tables, made once for the session."""
        reuse_databases = True if pytestconfig.getoption("hare_reuse_db") else None
        async with TemporaryDatabases(
            HareFixtures.get_config(pytestconfig),
            HareFixtures.get_db_url(pytestconfig),
            reuse_databases=reuse_databases,
        ) as context:
            yield context

    @pytest_asyncio.fixture(loop_scope="session")
    async def hare_rollback_isolation(self, hare_database: HareContext) -> AsyncGenerator[RollbackIsolation]:
        """The test's writes rolled back when it ends."""
        isolation = RollbackIsolation(hare_database)
        async with isolation:
            yield isolation

    @pytest.fixture
    def hare_db(self, hare_database: HareContext, hare_rollback_isolation: RollbackIsolation) -> HareContext:
        """The context of a test whose writes are rolled back when it ends."""
        return hare_database

    @pytest_asyncio.fixture(loop_scope="session")
    async def hare_transactional_db(self, hare_database: HareContext) -> AsyncGenerator[HareContext]:
        """The context of a test whose writes really commit - ``on_commit()`` callbacks run - and
        whose tables are emptied when it ends."""
        token = HareContext.current_context.set(hare_database)
        try:
            yield hare_database
        finally:
            await truncate_all_models(context=hare_database)
            HareContext.current_context.reset(token)

    @pytest.fixture
    def hare_assert_query_count(self) -> Callable[..., Any]:
        """``assert_query_count()`` - ``async with hare_assert_query_count(1): ...``."""
        return assert_query_count

    @pytest.fixture
    def hare_capture_on_commit(self, hare_rollback_isolation: RollbackIsolation) -> Callable[..., Any]:
        """The isolation's ``capture_on_commit()`` - ``async with hare_capture_on_commit() as callbacks``."""
        return hare_rollback_isolation.capture_on_commit

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_call(self, item: pytest.Item) -> None:
        """Skips a test marked ``hare_requires`` whose connection lacks the features named."""
        marker = item.get_closest_marker(HARE_REQUIRES_MARKER)
        if marker is None:
            return
        fixture_values = getattr(item, "funcargs", {})
        context = next((fixture_values[name] for name in CONTEXT_FIXTURE_NAMES if name in fixture_values), None)
        if context is None:
            raise pytest.UsageError(
                f"@pytest.mark.{HARE_REQUIRES_MARKER} on {item.nodeid} needs one of the fixtures "
                f"{CONTEXT_FIXTURE_NAMES}"
            )
        conditions = dict(marker.kwargs)
        connection = FeatureConditions.get_connection(context, conditions.pop("connection_alias", None))
        mismatch = FeatureConditions.get_mismatch(connection, conditions)
        if mismatch is not None:
            pytest.skip(mismatch)
