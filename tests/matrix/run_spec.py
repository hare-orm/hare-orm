from __future__ import annotations

from dataclasses import dataclass

from tests.matrix.constants import DATABASE_INDEPENDENT_MARKER
from tests.matrix.suite_environment import SuiteEnvironment


@dataclass(frozen=True, slots=True)
class RunSpec:
    """Inputs of one matrix process, with explicit physical resources and cleanup scope.

    Resources name the catalog's physical servers, independent of driver or URL. A pooler
    profile reserves its direct backend too. Cluster profiles reserve all their nodes as one
    cluster resource. No two runs sharing a resource may execute at the same time.
    """

    python_version: str
    suite: str
    suite_environment: SuiteEnvironment
    database: str
    environment: dict[str, str]
    test_paths: tuple[str, ...] = ()
    marker_expression: str = f"not {DATABASE_INDEPENDENT_MARKER}"
    resources: frozenset[str] = frozenset()
    allow_empty: bool = False
    sqlite_version: str | None = None

    @property
    def few_tests(self) -> bool:
        return not self.suite_environment.whole_suite and not self.resources
