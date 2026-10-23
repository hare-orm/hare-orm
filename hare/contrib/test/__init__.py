"""Testing helpers: ``hare_test_context()`` for an isolated database per fixture,
``requires_features()`` to skip by capability.

Example::

    @pytest_asyncio.fixture
    async def db():
        async with hare_test_context(["myapp.models"]) as context:
            yield context
"""

from __future__ import annotations

from hare.contrib.test.constants import MEMORY_SQLITE
from hare.contrib.test.databases.model_truncation import topological_sort_models, truncate_all_models
from hare.contrib.test.databases.reusable_test_databases import ReusableTestDatabases
from hare.contrib.test.databases.rollback_isolation import RollbackIsolation
from hare.contrib.test.databases.temporary_databases import TemporaryDatabases
from hare.contrib.test.features.feature_requirements import requires_features
from hare.contrib.test.isolated_contexts import hare_test_context, init_memory_sqlite
from hare.contrib.test.queries.query_counter import QueryCounter
from hare.contrib.test.queries.query_counting import assert_query_count, capture_queries
from hare.core.hare_context import HareContext

__all__ = (
    "MEMORY_SQLITE",
    "HareContext",
    "ReusableTestDatabases",
    "RollbackIsolation",
    "TemporaryDatabases",
    "hare_test_context",
    "requires_features",
    "truncate_all_models",
    "topological_sort_models",
    "init_memory_sqlite",
    "QueryCounter",
    "capture_queries",
    "assert_query_count",
)
