from __future__ import annotations

import pytest

from hare.contrib.pytest.constants import PYTEST_ASYNCIO_MISSING_MESSAGE


class MissingPytestAsyncioFixtures:
    """Stands in for the fixtures of hare's pytest plugin without pytest-asyncio: a test using one
    fails with what to install, not with an unknown fixture."""

    @pytest.fixture(scope="session")
    def hare_database(self) -> None:
        pytest.fail(PYTEST_ASYNCIO_MISSING_MESSAGE, pytrace=False)

    @pytest.fixture
    def hare_rollback_isolation(self) -> None:
        pytest.fail(PYTEST_ASYNCIO_MISSING_MESSAGE, pytrace=False)

    @pytest.fixture
    def hare_db(self) -> None:
        pytest.fail(PYTEST_ASYNCIO_MISSING_MESSAGE, pytrace=False)

    @pytest.fixture
    def hare_transactional_db(self) -> None:
        pytest.fail(PYTEST_ASYNCIO_MISSING_MESSAGE, pytrace=False)

    @pytest.fixture
    def hare_assert_query_count(self) -> None:
        pytest.fail(PYTEST_ASYNCIO_MISSING_MESSAGE, pytrace=False)

    @pytest.fixture
    def hare_capture_on_commit(self) -> None:
        pytest.fail(PYTEST_ASYNCIO_MISSING_MESSAGE, pytrace=False)
