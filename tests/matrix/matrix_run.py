from __future__ import annotations

import dataclasses

from tests.matrix.constants import DATABASE_INDEPENDENT_MARKER, DATABASE_INDEPENDENT_SUITE, SQLITE_SUITE_PREFIX
from tests.matrix.suite_environment import SuiteEnvironment


@dataclasses.dataclass
class MatrixRun:
    """One cell of the test matrix: the tests of an environment in a suite of a database, on one Python
    version, and its outcome.

    Attributes:
        python_version: The Python the suite runs on.
        suite: The suite's name.
        suite_environment: The environment whose tests run - and whose virtualenv runs them.
        database: The database of the suite - ``sqlite`` or a key of ``MATRIX_DATABASE_DRIVER_EXTRAS``, its
            driver's extra joining the virtualenv.
        environment: The environment variables of the run - the database among them.
        test_paths: The tests of the suite; empty for all.
        marker_expression: pytest's ``-m`` of the suite - the suite of a database leaves out the
            tests reading none.
        exit_code: pytest's exit code, None until the run ended.
        counts: pytest's summary counts by outcome.
        duration_seconds: How long the run took.
    """

    python_version: str
    suite: str
    suite_environment: SuiteEnvironment
    database: str
    environment: dict[str, str]
    test_paths: tuple[str, ...] = ()
    marker_expression: str = f"not {DATABASE_INDEPENDENT_MARKER}"
    exit_code: int | None = None
    counts: dict[str, int] = dataclasses.field(default_factory=dict)
    duration_seconds: float | None = None

    def reaches_a_server(self) -> bool:
        """Whether the run's tests reach a database or broker server - beside another run on it, each would
        drop the other's test databases; the tests reading no database and the SQLite ones reach none.

        Returns:
            True for a suite of a server.
        """
        return not (self.suite == DATABASE_INDEPENDENT_SUITE or self.suite.startswith(SQLITE_SUITE_PREFIX))
