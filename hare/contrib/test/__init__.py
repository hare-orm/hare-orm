"""Testing helpers: ``hare_test_context()`` for an isolated database per fixture,
``requires_features()`` to skip by capability.

Example::

    @pytest_asyncio.fixture
    async def db():
        async with hare_test_context(["myapp.models"]) as ctx:
            yield ctx
"""

from unittest import SkipTest, expectedFailure, skip, skipIf, skipUnless

from hare.contrib.test.constants import MEMORY_SQLITE
from hare.contrib.test.helpers import (
    assert_num_queries,
    capture_queries,
    hare_test_context,
    init_memory_sqlite,
    requires_features,
    topological_sort_models,
    truncate_all_models,
)
from hare.contrib.test.query_counter import QueryCounter
from hare.contrib.test.reusable_databases import ReusableTestDatabases
from hare.core.context import HareContext

__all__ = (
    "MEMORY_SQLITE",
    "HareContext",
    "ReusableTestDatabases",
    "hare_test_context",
    "requires_features",
    "truncate_all_models",
    "topological_sort_models",
    "init_memory_sqlite",
    "QueryCounter",
    "capture_queries",
    "assert_num_queries",
    "SkipTest",
    "expectedFailure",
    "skip",
    "skipIf",
    "skipUnless",
)

expectedFailure.__doc__ = """
Mark test as expecting failure.

On success it will be marked as unexpected success.
"""
