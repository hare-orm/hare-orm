from __future__ import annotations

import argparse
import json
import sys
import tomllib
from itertools import product
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
    def get_unsupported_extras(cls, python_version: str) -> frozenset[str]:
        """Extras unavailable on one Python, shared by installation and test selection."""
        return frozenset(cls.read().get("unsupported_extras", {}).get(python_version, ()))

    @classmethod
    def get_environments(cls, python_version: str | None = None) -> tuple[SuiteEnvironment, ...]:
        """Expand the available environments and their aggregate for a selected Python.

        Without a Python, return every declaration for manifest validation. A selected
        Python omits unavailable extras and database drivers before installation.
        """
        unsupported = cls.get_unsupported_extras(python_version) if python_version is not None else frozenset()
        driver_extras = cls.get("database_driver_extras")
        environments: list[SuiteEnvironment] = []
        for declaration in cls.get("environments"):
            values: dict[str, Any] = {
                name: tuple(value) if isinstance(value, list) else value for name, value in declaration.items()
            }
            if values.pop("all_extras", False):
                values["extras"] = tuple(
                    dict.fromkeys(extra for environment in environments for extra in environment.extras)
                )
                values["groups"] = tuple(
                    dict.fromkeys(group for environment in environments for group in environment.groups)
                )
            elif unsupported.intersection(values.get("extras", ())):
                continue
            if "databases" in values:
                values["databases"] = tuple(
                    database
                    for database in values["databases"]
                    if not unsupported.intersection(driver_extras[database])
                )
            environments.append(SuiteEnvironment(**values))
        return tuple(environments)

    @classmethod
    def get_latest_python(cls, environment_name: str, database: str) -> str | None:
        """The newest Python on which this environment can exercise the database."""
        for python_version in reversed(cls.get("versions.python")):
            for environment in cls.get_environments(python_version):
                if environment.name == environment_name and database in environment.databases:
                    return python_version
        return None

    @classmethod
    def get_ci_matrices(cls) -> dict[str, dict[str, Any]]:
        """Expand shared version axes while retaining the explicitly reduced CI profiles; a profile
        naming ``platforms`` includes those entries of ``wheel_platforms``."""
        matrices = {}
        for name, profile in cls.get("ci").items():
            matrix = {axis: cls.get(path) for axis, path in profile.get("axes", {}).items()}
            if "platforms" in profile:
                matrix["include"] = [
                    platform for platform in cls.get("wheel_platforms") if platform["platform"] in profile["platforms"]
                ]
            matrix.update({axis: cls.get(path)[-1:] for axis, path in profile.get("newest_axes", {}).items()})
            if drivers := profile.get("drivers"):
                combinations = [dict(zip(matrix, values, strict=True)) for values in product(*matrix.values())]
                matrix = {
                    "include": [
                        {
                            **combination,
                            "driver": driver,
                            "python": cls.get_latest_python("all", cls.get("databases_by_driver")[driver]),
                        }
                        for combination in combinations
                        for driver in cls.get(drivers)
                    ]
                }
                matrix["include"].extend(profile.get("matrix", {}).get("include", ()))
            else:
                matrix.update(profile.get("matrix", {}))
            matrices[name] = matrix
        return matrices

    @classmethod
    def main(cls) -> None:
        """Print catalog data as JSON or Make's whitespace-separated values."""
        parser = argparse.ArgumentParser(description="Read the shared test matrix catalog.")
        parser.add_argument("path", help="Dotted catalog path, for example versions.python or images.pgbouncer")
        parser.add_argument("--json", action="store_true", help="Emit structured JSON for CI")
        parser.add_argument("--python-version", default=f"{sys.version_info.major}.{sys.version_info.minor}")
        parser.add_argument("--prefix", default="", help="Prefix each selected extra for installer arguments")
        arguments = parser.parse_args()
        value: Any
        if arguments.path == "extras":
            environment = next(item for item in cls.get_environments(arguments.python_version) if item.name == "all")
            value = [arguments.prefix + extra for extra in environment.extras]
        else:
            value = cls.get_ci_matrices() if arguments.path == "ci" else cls.get(arguments.path)
        sys.stdout.reconfigure(newline="\n")  # type: ignore[union-attr]
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
