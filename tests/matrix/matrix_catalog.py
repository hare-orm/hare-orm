from __future__ import annotations

import argparse
import json
import sys
import tomllib
from typing import Any, ClassVar

from tests.matrix.paths import MATRIX_CATALOG_PATH
from tests.matrix.suite_environment import SuiteEnvironment


class MatrixCatalog:
    """One stdlib-readable description of supported test environments and server profiles.

    The catalog can be read before installing hare or pytest, including from Make and CI.
    The all-extras environment expands the declared extras in their original order; no
    duplicate version or package lists need to be maintained in runner code.
    """

    data: ClassVar[dict[str, Any] | None] = None

    @classmethod
    def read(cls) -> dict[str, Any]:
        """Load the process's fixed configuration once, without importing test modules."""
        if cls.data is None:
            with MATRIX_CATALOG_PATH.open("rb") as source:
                cls.data = tomllib.load(source)
        return cls.data

    @classmethod
    def get(cls, path: str) -> Any:
        """Read a dotted catalog path; quoted version keys stay inside their parent table."""
        value: Any = cls.read()
        for component in path.split("."):
            value = value[component]
        return value

    @classmethod
    def get_environments(cls) -> tuple[SuiteEnvironment, ...]:
        """Construct environment declarations, expanding the final all-extras entry."""
        environments = []
        for declaration in cls.get("environments"):
            values = {name: tuple(value) if isinstance(value, list) else value for name, value in declaration.items()}
            if values.pop("all_extras", False):
                values["extras"] = tuple(
                    dict.fromkeys(extra for environment in environments for extra in environment.extras)
                )
                values["groups"] = tuple(
                    dict.fromkeys(group for environment in environments for group in environment.groups)
                )
            environments.append(SuiteEnvironment(**values))
        return tuple(environments)

    @classmethod
    def get_ci_matrices(cls) -> dict[str, dict[str, Any]]:
        """Expand shared version axes while retaining the explicitly reduced CI profiles."""
        matrices = {}
        for name, profile in cls.get("ci").items():
            matrix = {axis: cls.get(path) for axis, path in profile.get("axes", {}).items()}
            matrix.update({axis: cls.get(path)[-1:] for axis, path in profile.get("newest_axes", {}).items()})
            matrix.update(profile.get("matrix", {}))
            matrices[name] = matrix
        return matrices

    @classmethod
    def main(cls) -> None:
        """Print catalog data as JSON or Make's whitespace-separated values."""
        parser = argparse.ArgumentParser(description="Read the shared test matrix catalog.")
        parser.add_argument("path", help="Dotted catalog path, for example versions.python or images.pgbouncer")
        parser.add_argument("--json", action="store_true", help="Emit structured JSON for CI")
        arguments = parser.parse_args()
        value = cls.get_ci_matrices() if arguments.path == "ci" else cls.get(arguments.path)
        sys.stdout.reconfigure(newline="\n")
        if arguments.json:
            print(json.dumps(value))
        elif isinstance(value, dict):
            print(" ".join(f"{name}={item}" for name, item in value.items()))
        elif isinstance(value, list):
            print(" ".join(map(str, value)))
        else:
            print(value)


if __name__ == "__main__":
    MatrixCatalog.main()
