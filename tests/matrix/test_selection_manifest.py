from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path

from tests.matrix.paths import REPOSITORY_DIRECTORY, TESTS_DIRECTORY
from tests.matrix.test_module_requirements import TestModuleRequirements


class TestSelectionManifest:
    """Read-only requirements used before imports and when validating collected cells.

    ``requires`` must be installed before importing the module. ``cells`` lists the
    optional requirements its parametrized tests may add. Cell marks choose among
    these declarations; arbitrary strings or imported Python definitions are never
    interpreted to decide which environment should collect a module.
    """

    __test__ = False

    def __init__(self, modules: Mapping[str, TestModuleRequirements], scopes: Mapping[str, frozenset[str]]) -> None:
        self.modules = modules
        self.scopes = scopes

    @classmethod
    def read(cls, path: Path) -> TestSelectionManifest:
        """Read the explicit module inventory and directory import requirements."""
        with path.open("rb") as source:
            data = tomllib.load(source)
        return cls(
            {
                name: TestModuleRequirements(frozenset(value["requires"]), frozenset(value["cells"]))
                for name, value in data["modules"].items()
            },
            {name: frozenset(requirements) for name, requirements in data.get("scopes", {}).items()},
        )

    def get_path_requirements(self, path: Path) -> frozenset[str]:
        """Get requirements without reading or importing any test or conftest source."""
        path = path.absolute()
        if not path.is_relative_to(TESTS_DIRECTORY):
            return frozenset()
        relative = path.relative_to(REPOSITORY_DIRECTORY).as_posix()
        inherited = frozenset(
            requirement
            for scope, requirements in self.scopes.items()
            if relative == scope or relative.startswith(scope + "/")
            for requirement in requirements
        )
        if relative in self.modules:
            return self.modules[relative].requires | inherited
        if path.name.startswith("test_") and path.suffix == ".py":
            raise ValueError(f"{relative}: declare the test module in tests/matrix/selection.toml")
        return inherited

    def check_inventory(self) -> None:
        """Detect newly added or deleted test modules even in a selective extras run."""
        actual = {path.relative_to(REPOSITORY_DIRECTORY).as_posix() for path in TESTS_DIRECTORY.rglob("test_*.py")}
        declared = set(self.modules)
        if actual != declared:
            raise ValueError(
                "Update tests/matrix/selection.toml: "
                f"undeclared modules={sorted(actual - declared)}, missing files={sorted(declared - actual)}"
            )

    def check_cell_requirements(self, path: Path, requirements: set[str] | frozenset[str]) -> None:
        """Refuse a collected cell whose requirements were not available to preselection."""
        relative = path.absolute().relative_to(REPOSITORY_DIRECTORY).as_posix()
        module = self.modules[relative]
        unexpected = requirements - self.get_path_requirements(path) - module.cells
        if unexpected:
            raise ValueError(f"{relative}: undeclared cell requirements {sorted(unexpected)} in selection.toml")
