from __future__ import annotations

import dataclasses

from tests.matrix.constants import DATABASE_INDEPENDENT_MARKER


@dataclasses.dataclass
class MatrixRun:
    """One cell of the test matrix: a test suite on one Python version, and its outcome.

    Attributes:
        python_version: The Python the suite runs on.
        suite: The suite's name.
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
    environment: dict[str, str]
    test_paths: tuple[str, ...] = ()
    marker_expression: str = f"not {DATABASE_INDEPENDENT_MARKER}"
    exit_code: int | None = None
    counts: dict[str, int] = dataclasses.field(default_factory=dict)
    duration_seconds: float | None = None
