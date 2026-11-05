from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence

from tests.matrix.matrix_catalog import MatrixCatalog


class CIResults:
    """Require every expected job to succeed, except the explicit dev-to-main test reduction."""

    @staticmethod
    def get_errors(
        jobs: Mapping[str, Mapping[str, object]], expected: Sequence[str], *, skip_tests: bool
    ) -> list[str]:
        errors = []
        if set(jobs) != set(expected):
            errors.append(
                f"Unexpected CI job set: missing={sorted(set(expected) - set(jobs))}, "
                f"extra={sorted(set(jobs) - set(expected))}"
            )
        for name, job in jobs.items():
            required = "skipped" if skip_tests and name != "catalog" else "success"
            if job.get("result") != required:
                errors.append(f"{name}: expected {required}, got {job.get('result')}")
        return errors

    @classmethod
    def main(cls) -> None:
        errors = cls.get_errors(
            json.loads(os.environ["CI_JOB_RESULTS"]),
            MatrixCatalog.get("ci_gate.jobs"),
            skip_tests=os.environ.get("CI_SKIP_TESTS") == "true",
        )
        for error in errors:
            print(f"::error::{error}")
        raise SystemExit(bool(errors))


if __name__ == "__main__":
    CIResults.main()
